"""End-to-end reproducibility check for an attested compose_hash.

Walks the chain of trust:

    on-chain compose_hash
      → live ``app_compose`` (fetched from the dstack tcb-info page on
        port 8090; ``sha256(app_compose)`` IS the compose_hash, so this
        step is cryptographically self-verifying)
      → ``docker_compose_file`` (the YAML embedded inside app_compose)
      → repo YAML at the on-chain-registered ``git_sha`` (downloaded from
        GitHub raw)
      → image references (``ghcr.io/.../<short_sha>``) inside the YAML

Each step is independently verifiable with ``curl`` + ``sha256sum`` if
the user wants to repeat it by hand. The CLI bundles them so a single
``hivemind trust attest --reproduce`` walks every link and reports
which ones held.

Pattern adapted from sxysun/is-this-real-tea's ``verify-compose-hash.py``
— same key insight that the dstack 8090 page exposes the raw
``app_compose`` string whose sha256 is the compose_hash, so no Phala
Cloud API key (and no dstack-formula re-derivation) is needed for
verification.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

# Short helper for the gateway URL — the friendly URL is fronted by
# dstack-ingress and doesn't expose the tcb-info page; the raw gateway
# does. Inferred from the bundle's ``pinning_url`` when present.
_DEFAULT_GATEWAY = "dstack-pha-prod9.phala.network"


def gateway_from_pinning_url(pinning_url: str) -> str:
    """Extract the gateway domain from a Tier-3 pinning URL.

    pinning_url shape: ``https://<app_id>-<port>s.<gateway>``. We need
    the gateway part (``dstack-pha-prod9.phala.network``) so we can
    construct the parallel ``-8090.<gateway>`` URL where the dstack
    tcb-info page lives.
    """
    m = re.match(r"^https?://[^.]+\.(.+)$", pinning_url.rstrip("/"))
    if not m:
        return _DEFAULT_GATEWAY
    return m.group(1)


def fetch_tcb_info(
    app_id: str,
    gateway: str,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Fetch the dstack tcb-info JSON exposed on the gateway's 8090 page.

    The page is plain HTML with the JSON in a ``<textarea>``. Returns
    the parsed dict, including the raw ``app_compose`` string (which
    hashes to compose_hash) and the claimed compose_hash.

    Raises ``httpx.HTTPError`` on transport failure and ``ValueError``
    if the textarea isn't present.
    """
    url = f"https://{app_id}-8090.{gateway}/"
    r = httpx.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "hivemind-cli/reproduce"},
    )
    r.raise_for_status()
    body = r.text
    start_tag = "<textarea readonly>"
    s = body.find(start_tag)
    e = body.find("</textarea>", s) if s >= 0 else -1
    if s < 0 or e < 0:
        raise ValueError(f"tcb_info textarea not found at {url}")
    raw = html.unescape(body[s + len(start_tag) : e])
    return json.loads(raw)


def verify_app_compose_hash(app_compose_str: str, claimed_hash: str) -> str:
    """Return ``sha256(app_compose_str)`` for comparison against ``claimed_hash``.

    The caller does the equality check (case-insensitive). We return the
    computed hash rather than a bool so the UI can show both values.
    """
    return hashlib.sha256(app_compose_str.encode("utf-8")).hexdigest()


def parse_app_compose(app_compose_str: str) -> dict[str, Any]:
    """Parse the JSON inside ``app_compose`` and return the dict."""
    return json.loads(app_compose_str)


def _strip_query(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _query_params(url: str) -> dict[str, str]:
    params = parse_qs(urlsplit(url).query, keep_blank_values=False)
    return {k: v[-1] for k, v in params.items() if v}


def blob_to_raw(github_blob_url: str) -> str | None:
    """Convert a GitHub blob URL to the raw-content URL.

    ``https://github.com/X/Y/blob/REF/path`` →
    ``https://raw.githubusercontent.com/X/Y/REF/path``. Returns ``None``
    on URLs that don't match the blob shape (e.g. gist, gitlab) so the
    caller can fall back to "show URL, ask user to verify by eye".
    """
    m = re.match(
        r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$",
        _strip_query(github_blob_url),
    )
    if not m:
        return None
    owner, repo, ref, path = m.group(1), m.group(2), m.group(3), m.group(4)
    return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"


def _parse_blob_url(blob_url: str) -> tuple[str, str, str, str] | None:
    """Return ``(owner, repo, ref, path)`` for a GitHub blob URL."""
    m = re.match(
        r"^https://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.+)$",
        _strip_query(blob_url),
    )
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3), m.group(4)


def _fetch_via_gh_cli(
    owner: str, repo: str, ref: str, path: str
) -> str | None:
    """Use the user's authenticated ``gh`` CLI to fetch a file's contents.

    Falls back to ``None`` if ``gh`` isn't installed, the user isn't
    logged in, or the call fails for any other reason. The caller then
    surfaces a manual-verification instruction. Tried after the
    anonymous raw.github fetch fails (which is the common case for
    private repos).
    """
    if not shutil.which("gh"):
        return None
    try:
        r = subprocess.run(
            [
                "gh",
                "api",
                "-X",
                "GET",
                f"/repos/{owner}/{repo}/contents/{path}?ref={ref}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        body = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    enc = body.get("encoding", "")
    content = body.get("content", "")
    if enc != "base64" or not content:
        return None
    try:
        return base64.b64decode(content).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def _fetch_via_github_api(
    owner: str, repo: str, ref: str, path: str, token: str
) -> str | None:
    """Fetch via the GitHub Contents API with an explicit token.

    Used when ``GITHUB_TOKEN`` / ``GH_TOKEN`` is in the environment but
    ``gh`` isn't installed. Returns the decoded text or ``None`` on any
    error so the caller can fall through to manual instructions.
    """
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    try:
        r = httpx.get(
            url,
            params={"ref": ref},
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "hivemind-cli/reproduce",
            },
            timeout=15,
        )
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    body = r.json()
    if body.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(body.get("content", "")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def fetch_repo_yaml(blob_url: str, *, timeout: float = 15.0) -> str:
    """Download the YAML at a GitHub blob URL.

    Tries in order: anonymous ``raw.githubusercontent.com`` (works for
    public repos), the authenticated GitHub Contents API (using
    ``GH_TOKEN`` / ``GITHUB_TOKEN`` if set), then ``gh api`` shelling
    out to the user's CLI auth. Raises ``ValueError`` with a clear
    message if all paths fail so the caller can surface manual steps.
    """
    parsed = _parse_blob_url(blob_url)
    if not parsed:
        raise ValueError(f"unrecognized GitHub blob URL: {blob_url}")
    owner, repo, ref, path = parsed
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"
    try:
        r = httpx.get(
            raw,
            timeout=timeout,
            headers={"User-Agent": "hivemind-cli/reproduce"},
        )
        if r.status_code == 200:
            return r.text
    except httpx.HTTPError:
        pass

    token = (
        os.environ.get("GITHUB_TOKEN", "")
        or os.environ.get("GH_TOKEN", "")
    ).strip()
    if token:
        out = _fetch_via_github_api(owner, repo, ref, path, token)
        if out is not None:
            return out

    out = _fetch_via_gh_cli(owner, repo, ref, path)
    if out is not None:
        return out

    raise ValueError(
        f"could not fetch {raw} (private repo? try `gh auth login` or "
        "set GITHUB_TOKEN, then re-run --reproduce)"
    )


def _replace_core_image(yaml_text: str, image_ref: str) -> str:
    pattern = re.compile(
        r"(^\s*image:\s*)"
        r"ghcr\.io/teleport-computer/hivemind-core(?::[^\s#]+|@[^\s#]+)?"
        r"([^\S\r\n]*(?:#.*)?$)",
        re.MULTILINE,
    )
    rendered, count = pattern.subn(
        lambda m: f"{m.group(1)}{image_ref}{m.group(2)}",
        yaml_text,
        count=1,
    )
    if count != 1:
        raise ValueError(
            "registered compose render requested a core image override, "
            "but no ghcr.io/teleport-computer/hivemind-core image line was found"
        )
    return rendered


def render_registered_compose(
    compose_uri: str,
    yaml_text: str,
) -> tuple[str, list[str]]:
    """Apply deterministic render hints carried by the on-chain URI.

    The deploy workflow can register a source pointer like:

        .../docker-compose.core.yaml?image_sha=3c3fcbb

    That says "use the repo YAML at this commit, then rewrite the core
    service image to the exact tag deployed by CI". The query string is
    part of the on-chain signed source metadata, so the verifier is not
    accepting an implicit local mutation.
    """
    params = _query_params(compose_uri)
    core_image = params.get("core_image", "").strip()
    image_sha = params.get("image_sha", "").strip()
    if core_image:
        return (
            _replace_core_image(yaml_text, core_image),
            [f"core image override: {core_image}"],
        )
    if image_sha:
        image_ref = f"ghcr.io/teleport-computer/hivemind-core:{image_sha}"
        return (
            _replace_core_image(yaml_text, image_ref),
            [f"core image tag override: {image_sha}"],
        )
    return yaml_text, []


def short_source(git_commit: str, compose_uri: str) -> str:
    """Format a one-line `owner/repo@<short_sha>:path` summary.

    Falls back to the bare URI on URLs we can't parse (gist, gitlab,
    raw git URLs, etc.). When ``git_commit`` is a sentinel like
    ``"reconcile"`` (the reconcile-hash workflow's placeholder), we
    pass it through verbatim instead of truncating to 7 chars.
    """
    m = re.match(
        r"^https://github\.com/([^/]+)/([^/]+)/blob/[^/]+/(.+)$",
        _strip_query(compose_uri),
    )
    short = git_commit[:7] if all(c in "0123456789abcdef" for c in git_commit.lower()) else git_commit
    if m:
        owner, repo, path = m.group(1), m.group(2), m.group(3)
        return f"{owner}/{repo}@{short}:{path}"
    return f"@{short}: {compose_uri}"


def extract_image_refs(yaml_text: str) -> list[str]:
    """Pull every ``image:`` value out of a docker-compose YAML.

    Naive line-based scan — good enough for our compose files where
    each service uses the literal ``image: <ref>`` form. Multi-line
    quoted values are not supported, but we don't use them.
    """
    refs: list[str] = []
    for line in yaml_text.splitlines():
        m = re.match(r"^\s*image:\s*([^\s#]+)", line)
        if m:
            ref = m.group(1).strip().strip('"').strip("'")
            if ref:
                refs.append(ref)
    return refs


__all__ = [
    "gateway_from_pinning_url",
    "fetch_tcb_info",
    "verify_app_compose_hash",
    "parse_app_compose",
    "blob_to_raw",
    "fetch_repo_yaml",
    "render_registered_compose",
    "short_source",
    "extract_image_refs",
]
