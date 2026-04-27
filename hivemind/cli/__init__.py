"""Hivemind CLI — composition root.

This package was refactored out of a single 4112-line ``cli.py`` so each
subcommand group lives in its own sub-module under ``~1000`` lines:

* ``_root.py`` — the bare ``@click.group()`` that owns global flags.
* ``_http.py`` — pinned httpx wrappers + per-host TLS pin cache.
* ``_config.py`` — profile / config / header helpers.
* ``_trust.py`` — remote attestation, DCAP, on-chain governance gate.
* ``_shared.py`` — query helpers (sync/async/poll), hmq:// URI parser.
* ``owner.py`` — ``init``, ``scope``, ``load``, ``share``, ``schema``,
  ``rotate-key``.
* ``recipient.py`` — ``ask``, ``query``, ``run``, ``runs``.
* ``agents.py`` — ``agents`` subcommand group.
* ``tokens.py`` — ``tokens`` subcommand group.
* ``profile.py`` — ``profile`` subcommand group.
* ``admin.py`` — ``admin`` subcommand group.
* ``trust_cmds.py`` — ``trust`` subcommand group.

This file pulls them all together and registers every command on the
root ``cli`` group, then re-exports the symbols the test suite reaches
into via ``from hivemind import cli as _cli_mod``:

* ``_cli_mod.cli`` — the click root group (entry point).
* ``_cli_mod._parse_hmq_uri`` — used by tests/test_cli_share.py.
* ``_cli_mod.httpx`` — used by tests/test_cli_trust.py for monkey-
  patching ``httpx.get``.

Plus the path constants ``_HIVEMIND_HOME`` / ``_PROFILES_DIR`` /
``_ACTIVE_POINTER`` and the ``_fetch_attestation`` helper, which the
trust-flow tests redirect via ``monkeypatch.setattr(hivemind.cli, ...)``.
The submodules look these up via deferred ``from . import ...`` at
call time so the patches actually affect the live call sites.
"""

from pathlib import Path

# Re-exported for tests: tests/test_cli_trust.py monkey-patches
# ``_cli_mod.httpx`` to swap ``httpx.get`` / ``httpx.ConnectError``
# during trust-flow scenarios.
import httpx  # noqa: F401  (test contract)

# ── Test-patchable path constants ──
#
# The CLI's persistent state lives under ``~/.hivemind`` by default.
# Tests redirect these into a tmp_path sandbox via:
#   monkeypatch.setattr(hivemind.cli, "_HIVEMIND_HOME", tmp_path / ...)
# Submodules look up these names dynamically (``from . import ...``)
# inside each function so the patch takes effect on the next call.
_HIVEMIND_HOME = Path.home() / ".hivemind"
_PROFILES_DIR = _HIVEMIND_HOME / "profiles"
_ACTIVE_POINTER = _HIVEMIND_HOME / "active"

# ── Test-patchable HTTP wrappers ──
#
# tests/test_cli_share.py monkey-patches ``_cli_mod._hget`` / ``_hpost``
# / ``_hdelete`` to redirect HTTP onto an in-process Starlette
# TestClient. Submodules call these via deferred ``from . import _hget``
# trampolines so the patch is picked up at call time.
from ._http import _hdelete, _hget, _hpost  # noqa: F401  (test contract)

# ── Subcommand modules ──
from . import admin, agents, owner, profile, recipient, tokens, trust_cmds
from ._root import cli
from ._shared import _parse_hmq_uri  # noqa: F401  (test contract)

# ── Test-patchable trust helper ──
#
# tests/test_cli_trust.py stubs ``_fetch_attestation`` to inject canned
# attestation bundles. Re-exported at parent-module level so the
# ``monkeypatch.setattr(hivemind.cli, "_fetch_attestation", ...)``
# patch hooks the same lookup the trust gate performs at call time.
from ._trust import _fetch_attestation  # noqa: F401  (test contract)


# ── Command registration ──
#
# Each submodule defines its commands as standalone ``@click.command()``
# (or sub-groups via ``@click.group(...)``); we attach them all to the
# root ``cli`` group here. This keeps the registration explicit + lets
# us rename a subcommand without touching the implementation file.

# Owner-side flow.
cli.add_command(owner.init)
cli.add_command(owner.scope)
cli.add_command(owner.load_cmd, "load")
cli.add_command(owner.share)
cli.add_command(owner.schema_cmd, "schema")
cli.add_command(owner.rotate_key, "rotate-key")

# Recipient / query flow.
cli.add_command(recipient.ask)
cli.add_command(recipient.query_cmd, "query")
cli.add_command(recipient.run_cmd, "run")
cli.add_command(recipient.runs_cmd, "runs")

# Subcommand groups.
cli.add_command(agents.agents_cli, "agents")
cli.add_command(tokens.tokens_cli, "tokens")
cli.add_command(profile.profile_cli, "profile")
cli.add_command(admin.admin_cli, "admin")
cli.add_command(trust_cmds.trust_group, "trust")


if __name__ == "__main__":
    cli()
