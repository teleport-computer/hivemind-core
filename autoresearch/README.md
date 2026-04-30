# autoresearch/

`autoresearch/` keeps the research log from the older agent experiments:
iteration notes, conclusions, ablation scripts, and historical results.

The retired GAN-style benchmark has been archived at
`autoresearch/legacy_bench/`. It is useful for reading the experiment history,
but it is not the active evaluation path. The active harness namespace is
[`eval/`](../eval/), which should use current room APIs and deterministic
graders.

Do not optimize new agents against the archived benchmark score. The old loop
used an LLM judge and produced shared-prior artifacts: implementations could
score better by matching the judge's implicit taxonomy rather than enforcing a
principled room policy.
