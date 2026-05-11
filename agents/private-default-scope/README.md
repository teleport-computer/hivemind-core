# private-default-scope

Same code as `default-scope`, but the *entire* scope prompt is supplied by the
host at upload time rather than pulled from a public template.

`agent.py` and `_bridge.py` are symlinked to `../default-scope/` — identical
binaries. The only thing that differs in the built image is `prompt.md`, whose
content is private to the host.

## Attestation frame

| Property | default-scope | private-default-scope |
|---|---|---|
| `agent.py` source | public (checked in) | public (checked in, via symlink) |
| `_bridge.py` source | public | public |
| Prompt framing (non-rule scaffolding) | public — `default-scope/scope-prompt.md` | **private** — host supplies whole file |
| Host's specific rules | private (fused at upload) | private (the whole prompt is) |
| Image digest | attestable | attestable |
| Silent prompt swap detectable? | ✅ (digest delta) | ✅ (digest delta) |

A visitor inspecting a deployed image gets:

```
hivemind-agent-<id>:latest
├── agent.py         (matches repo sha of default-scope/agent.py)
├── _bridge.py       (matches repo sha of default-scope/_bridge.py)
└── prompt.md        (opaque — host-supplied, TEE-resident)
```

They can verify the *processing code* byte-for-byte against the repo. They
cannot read the prompt. If the host rebuilds with different prompt text, the
image digest changes and the link's committed digest no longer matches — the
swap is cryptographically detectable without being readable.

## Usage

```bash
# Write your private scope rules (never checked into git)
cat > ~/private-rules.md <<'EOF'
You are a scope agent. Enforce these rules (CONFIDENTIAL):
- Only return counts >= 100
- Redact any row containing ...
...
EOF

# Upload manually:
cp ~/private-rules.md agents/private-default-scope/prompt.md
tar czf /tmp/agent.tar.gz -C agents/private-default-scope \
  Dockerfile agent.py _bridge.py prompt.md
curl -F "name=private-scope" \
     -F "agent_type=scope" \
     -F "inspection_mode=sealed" \
     -F "archive=@/tmp/agent.tar.gz;type=application/gzip" \
     -H "Authorization: Bearer $TENANT_API_KEY" \
     http://localhost:8100/v1/room-agents
rm agents/private-default-scope/prompt.md      # don't leave it on disk
```

## Why this folder exists

To serve as the concrete reference for Pattern A (build-time private input).
Any agent — scope, query, mediator — can use the same technique: put
the secret file in the build context, `COPY` it into the image, and let the
TEE + image digest do the rest. No new primitives, no secrets table, no
manifest schema.
