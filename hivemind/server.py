import asyncio
import logging
import os
import secrets
import shutil
import tarfile
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from .config import Settings
from .core import Hivemind
from .models import (
    HealthResponse,
    IndexRequest,
    IndexResponse,
    QueryRequest,
    QueryResponse,
    StoreRequest,
    StoreResponse,
)
from .sandbox.settings import build_sandbox_settings
from .version import APP_VERSION

logger = logging.getLogger(__name__)

_IGNORED_TAR_TYPES = {
    tarfile.XHDTYPE,         # PAX extended header
    tarfile.XGLTYPE,         # PAX global header
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
}

MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB compressed archive bytes
MAX_UPLOAD_TAR_MEMBERS = 2_000
MAX_UPLOAD_TAR_MEMBER_BYTES = 15 * 1024 * 1024  # 15 MB per file
MAX_UPLOAD_TAR_TOTAL_BYTES = 150 * 1024 * 1024  # 150 MB total extracted size

_UI_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hivemind Core</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'SF Mono','Fira Code','Cascadia Code',monospace;background:linear-gradient(160deg,#1a1a2e 0%,#16213e 30%,#0f3460 60%,#1a1a2e 100%);color:#c8d6e5;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 20px}
h1{font-size:1.3em;color:#54a0ff;margin-bottom:4px}
.sub{color:#778ca3;font-size:.72em;margin-bottom:24px}
.card{width:100%;max-width:700px;background:rgba(255,255,255,.07);border:1px solid rgba(84,160,255,.15);border-radius:10px;padding:20px;margin-bottom:16px;backdrop-filter:blur(6px)}
.card h2{font-size:.85em;color:#54a0ff;margin-bottom:12px}
label{display:block;font-size:.72em;color:#778ca3;margin-bottom:3px;margin-top:10px}
input[type=text],input[type=number]{width:100%;background:rgba(255,255,255,.08);border:1px solid rgba(84,160,255,.2);border-radius:6px;padding:8px 12px;color:#c8d6e5;font-family:inherit;font-size:.8em;outline:none}
input:focus{border-color:rgba(84,160,255,.5);box-shadow:0 0 12px rgba(84,160,255,.2)}
input[type=file]{font-size:.75em;color:#778ca3;margin-top:4px}
.row{display:flex;gap:10px}
.row>*{flex:1}
button{background:linear-gradient(135deg,rgba(84,160,255,.2),rgba(72,219,251,.15));border:1px solid rgba(84,160,255,.3);border-radius:8px;padding:10px 20px;color:#54a0ff;font-family:inherit;font-size:.82em;cursor:pointer;transition:all .2s;backdrop-filter:blur(4px)}
button:hover{background:linear-gradient(135deg,rgba(84,160,255,.35),rgba(72,219,251,.25));box-shadow:0 0 20px rgba(84,160,255,.2)}
button:disabled{opacity:.4;cursor:wait}
.toggle-adv{font-size:.7em;color:#778ca3;cursor:pointer;margin-top:8px;display:inline-block}
.toggle-adv:hover{color:#54a0ff}
.adv{display:none;margin-top:8px}
.adv.open{display:block}

/* Stage timeline */
.stages{margin-top:14px}
.stage-row{display:flex;align-items:center;gap:8px;margin-bottom:6px;font-size:.75em}
.stage-label{width:100px;text-align:right;color:#778ca3;font-weight:bold}
.stage-bar-wrap{flex:1;height:22px;background:rgba(255,255,255,.05);border-radius:5px;position:relative;overflow:hidden}
.stage-bar{height:100%;border-radius:5px;transition:width .3s ease;display:flex;align-items:center;padding-left:8px;font-size:.85em;color:rgba(0,0,0,.5)}
.stage-bar.build{background:rgba(119,140,163,.3)}
.stage-bar.scope{background:rgba(255,177,66,.3)}
.stage-bar.query{background:rgba(84,160,255,.3)}
.stage-bar.mediator{background:rgba(165,94,234,.3)}
.stage-bar.total{background:rgba(38,222,129,.25)}
.stage-time{width:60px;text-align:right;color:#778ca3;font-size:.85em}
.stage-status{width:20px;text-align:center}
.done{color:#26de81}
.running{color:#ffb142}
.pending-s{color:#4a5568}

/* Result */
.result-box{margin-top:12px}
.result-text{background:rgba(255,255,255,.05);border:1px solid rgba(84,160,255,.15);border-radius:8px;padding:14px;font-size:.8em;line-height:1.5;white-space:pre-wrap;max-height:400px;overflow-y:auto;color:#c8d6e5}
.dl-link{display:inline-block;margin-top:8px;color:#54a0ff;font-size:.75em;text-decoration:none;border:1px solid rgba(84,160,255,.3);padding:4px 10px;border-radius:5px}
.dl-link:hover{background:rgba(84,160,255,.15)}
.error-text{color:#fc5c65}
.meta{color:#4a5568;font-size:.65em;margin-top:6px}

/* Run history */
.run-item{display:flex;align-items:center;gap:8px;padding:6px 10px;margin-bottom:3px;border-radius:6px;cursor:pointer;background:rgba(255,255,255,.04);border:1px solid rgba(84,160,255,.1);transition:all .15s;font-size:.72em}
.run-item:hover{background:rgba(255,255,255,.08);border-color:rgba(84,160,255,.25)}
.run-id{color:#54a0ff;font-weight:bold;width:90px}
.run-name{flex:1;color:#778ca3;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.run-status{width:70px;text-align:center;padding:2px 6px;border-radius:4px;font-size:.9em}
.s-completed{background:rgba(38,222,129,.15);color:#26de81}
.s-running{background:rgba(255,177,66,.15);color:#ffb142}
.s-pending{background:rgba(119,140,163,.15);color:#778ca3}
.s-failed{background:rgba(252,92,101,.15);color:#fc5c65}
.run-time{color:#4a5568;width:80px;text-align:right}

.empty{color:#4a5568;font-size:.75em;text-align:center;padding:16px}
#status-panel{display:none}
</style>
</head>
<body>

<h1>Hivemind Core</h1>
<p class="sub">Query Agent Submit Pipeline</p>

<!-- Submit Form -->
<div class="card">
  <h2>Submit Query Agent</h2>
  <label>Agent Name</label>
  <input type="text" id="agent-name" placeholder="e.g. tiktok-analytics" value="tiktok-analytics">
  <label>Agent Archive (.tar.gz)</label>
  <input type="file" id="archive" accept=".tar.gz,.tgz,.gz,application/gzip,application/x-gzip,application/x-tar,application/x-compressed-tar">

  <span class="toggle-adv" onclick="document.getElementById('adv').classList.toggle('open')">&#9662; Advanced options</span>
  <div id="adv" class="adv">
    <div class="row">
      <div><label>Max LLM Calls</label><input type="number" id="max-llm" value="5" min="1"></div>
      <div><label>Max Tokens</label><input type="number" id="max-tokens" value="50000" min="1"></div>
      <div><label>Timeout (s)</label><input type="number" id="timeout" value="120" min="1"></div>
    </div>
  </div>

  <div style="margin-top:14px"><button id="submit-btn" onclick="submitAgent()">Submit</button></div>
</div>

<!-- Status Panel -->
<div class="card" id="status-panel">
  <h2>Run <span id="run-id-display"></span></h2>
  <div class="stages">
    <div class="stage-row">
      <span class="stage-label">Build</span>
      <div class="stage-bar-wrap"><div class="stage-bar build" id="bar-build"></div></div>
      <span class="stage-time" id="time-build">--</span>
      <span class="stage-status" id="icon-build">&#9679;</span>
    </div>
    <div class="stage-row">
      <span class="stage-label">Scope</span>
      <div class="stage-bar-wrap"><div class="stage-bar scope" id="bar-scope"></div></div>
      <span class="stage-time" id="time-scope">--</span>
      <span class="stage-status" id="icon-scope">&#9679;</span>
    </div>
    <div class="stage-row">
      <span class="stage-label">Query</span>
      <div class="stage-bar-wrap"><div class="stage-bar query" id="bar-query"></div></div>
      <span class="stage-time" id="time-query">--</span>
      <span class="stage-status" id="icon-query">&#9679;</span>
    </div>
    <div class="stage-row">
      <span class="stage-label">Mediator</span>
      <div class="stage-bar-wrap"><div class="stage-bar mediator" id="bar-mediator"></div></div>
      <span class="stage-time" id="time-mediator">--</span>
      <span class="stage-status" id="icon-mediator">&#9679;</span>
    </div>
    <div class="stage-row" style="margin-top:4px;border-top:1px solid rgba(84,160,255,.1);padding-top:6px">
      <span class="stage-label">Total</span>
      <div class="stage-bar-wrap"><div class="stage-bar total" id="bar-total"></div></div>
      <span class="stage-time" id="time-total">--</span>
      <span class="stage-status" id="icon-total">&#9679;</span>
    </div>
  </div>
  <div class="result-box" id="result-box" style="display:none">
    <div class="result-text" id="result-text"></div>
    <a class="dl-link" id="dl-link" href="#" target="_blank" style="display:none">Download Report</a>
  </div>
  <p class="meta" id="run-meta"></p>
</div>

<!-- Run History -->
<div class="card">
  <h2>Recent Runs</h2>
  <div id="run-list"><div class="empty">No runs yet</div></div>
</div>

<script>
const $ = id => document.getElementById(id);

function headers() { return {'Authorization':'Bearer NPSd_V3V9fyLvroWF9mS1ad1pH1cZceDFc2pq6Eirek'}; }

let pollTimer = null;
let currentRunId = null;

async function submitAgent() {
  const name = $('agent-name').value.trim();
  const file = $('archive').files[0];
  if (!name) return alert('Agent name is required');
  if (!file) return alert('Please select an archive file');

  const fd = new FormData();
  fd.append('name', name);
  fd.append('archive', file);
  fd.append('max_llm_calls', $('max-llm').value);
  fd.append('max_tokens', $('max-tokens').value);
  fd.append('timeout_seconds', $('timeout').value);

  $('submit-btn').disabled = true;
  $('submit-btn').textContent = 'Submitting...';

  try {
    const t0 = Date.now();
    const resp = await fetch('/v1/query-agents/submit', {method:'POST', headers: headers(), body: fd});
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({detail: resp.statusText}));
      throw new Error(err.detail || JSON.stringify(err));
    }
    const data = await resp.json();
    const uploadTime = ((Date.now() - t0) / 1000).toFixed(1);

    currentRunId = data.run_id;
    $('status-panel').style.display = 'block';
    $('run-id-display').textContent = data.run_id;
    $('result-box').style.display = 'none';
    $('dl-link').style.display = 'none';
    $('run-meta').textContent = 'Upload: ' + uploadTime + 's | Agent: ' + data.agent_id;
    resetBars();

    // Save to history
    saveRun({run_id: data.run_id, agent_id: data.agent_id, name, status: 'pending', created_at: Date.now()/1000});

    startPolling(data.run_id);
  } catch(e) {
    alert('Submit failed: ' + e.message);
  } finally {
    $('submit-btn').disabled = false;
    $('submit-btn').textContent = 'Submit';
  }
}

function resetBars() {
  for (const s of ['build','scope','query','mediator','total']) {
    $('bar-'+s).style.width = '0%';
    $('bar-'+s).textContent = '';
    $('time-'+s).textContent = '--';
    $('icon-'+s).innerHTML = '<span class="pending-s">&#9679;</span>';
  }
}

function startPolling(runId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(() => pollRun(runId), 2000);
  pollRun(runId);
}

async function pollRun(runId) {
  try {
    const resp = await fetch('/v1/query-agents/runs/' + runId, {headers: headers()});
    if (!resp.ok) return;
    const d = await resp.json();
    updateStages(d);
    updateRunInHistory(d);

    if (d.status === 'completed' || d.status === 'failed') {
      clearInterval(pollTimer);
      pollTimer = null;
      showResult(d);
    }
  } catch(e) { /* ignore poll errors */ }
}

function stageDuration(d, stage) {
  const s = d[stage+'_started_at'];
  const e = d[stage+'_ended_at'];
  if (!s) return null;
  if (!e) return {running: true, elapsed: (Date.now()/1000) - s};
  return {running: false, elapsed: e - s};
}

function fmtTime(sec) {
  if (sec < 60) return sec.toFixed(1) + 's';
  return Math.floor(sec/60) + 'm ' + (sec%60).toFixed(0) + 's';
}

function updateStages(d) {
  const totalElapsed = (d.updated_at || Date.now()/1000) - d.created_at;
  const maxBar = Math.max(totalElapsed, 1);

  for (const stage of ['build','scope','query','mediator']) {
    const info = stageDuration(d, stage);
    if (!info) {
      // not started yet
      $('icon-'+stage).innerHTML = '<span class="pending-s">&#9679;</span>';
      $('time-'+stage).textContent = '--';
      $('bar-'+stage).style.width = '0%';
      continue;
    }
    const pct = Math.min(100, (info.elapsed / maxBar) * 100);
    $('bar-'+stage).style.width = pct + '%';
    $('time-'+stage).textContent = fmtTime(info.elapsed);
    if (info.running) {
      $('icon-'+stage).innerHTML = '<span class="running">&#9684;</span>';
      $('bar-'+stage).textContent = '';
    } else {
      $('icon-'+stage).innerHTML = '<span class="done">&#10003;</span>';
    }
  }

  // Total
  const totalPct = d.status === 'completed' || d.status === 'failed' ? 100 : Math.min(95, (totalElapsed / (d.timeout_seconds||120)) * 100);
  $('bar-total').style.width = totalPct + '%';
  $('time-total').textContent = fmtTime(totalElapsed);
  if (d.status === 'completed') {
    $('icon-total').innerHTML = '<span class="done">&#10003;</span>';
  } else if (d.status === 'failed') {
    $('icon-total').innerHTML = '<span class="error-text">&#10007;</span>';
  } else {
    $('icon-total').innerHTML = '<span class="running">&#9684;</span>';
  }
}

function showResult(d) {
  $('result-box').style.display = 'block';
  if (d.status === 'failed') {
    $('result-text').innerHTML = '<span class="error-text">Failed: ' + esc(d.error || 'Unknown error') + '</span>';
  } else {
    let txt = d.output || '(no text output)';
    // Try to pretty-print JSON
    try { txt = JSON.stringify(JSON.parse(txt), null, 2); } catch(e) {}
    $('result-text').textContent = txt;
  }
  if (d.download_url) {
    $('dl-link').href = d.download_url;
    $('dl-link').style.display = 'inline-block';
  }
}

function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// ── Run History ──

function getHistory() {
  try { return JSON.parse(localStorage.getItem('hm_runs') || '[]'); } catch { return []; }
}

function saveRun(run) {
  const hist = getHistory().filter(r => r.run_id !== run.run_id);
  hist.unshift(run);
  if (hist.length > 50) hist.length = 50;
  localStorage.setItem('hm_runs', JSON.stringify(hist));
  renderHistory();
}

function updateRunInHistory(d) {
  const hist = getHistory();
  const idx = hist.findIndex(r => r.run_id === d.run_id);
  if (idx >= 0) {
    hist[idx] = {...hist[idx], ...d};
    localStorage.setItem('hm_runs', JSON.stringify(hist));
    renderHistory();
  }
}

function renderHistory() {
  const hist = getHistory();
  if (!hist.length) { $('run-list').innerHTML = '<div class="empty">No runs yet</div>'; return; }
  $('run-list').innerHTML = hist.map(r => {
    const sc = 's-' + (r.status || 'pending');
    const t = r.created_at ? new Date(r.created_at * 1000).toLocaleString() : '';
    return '<div class="run-item" onclick="viewRun(\\'' + r.run_id + '\\')">' +
      '<span class="run-id">' + r.run_id + '</span>' +
      '<span class="run-name">' + esc(r.name || '') + '</span>' +
      '<span class="run-status ' + sc + '">' + (r.status||'pending') + '</span>' +
      '<span class="run-time">' + t.split(', ').pop() + '</span></div>';
  }).join('');
}

async function viewRun(runId) {
  currentRunId = runId;
  $('status-panel').style.display = 'block';
  $('run-id-display').textContent = runId;
  $('result-box').style.display = 'none';
  $('dl-link').style.display = 'none';
  $('run-meta').textContent = '';
  resetBars();
  try {
    const resp = await fetch('/v1/query-agents/runs/' + runId, {headers: headers()});
    if (!resp.ok) return;
    const d = await resp.json();
    updateStages(d);
    if (d.status === 'completed' || d.status === 'failed') {
      showResult(d);
    } else {
      startPolling(runId);
    }
  } catch(e) {}
  $('status-panel').scrollIntoView({behavior:'smooth'});
}

// Load history on start
renderHistory();

// Also try to load from API
(async () => {
  try {
    const resp = await fetch('/v1/query-agents/runs?limit=20', {headers: headers()});
    if (resp.ok) {
      const runs = await resp.json();
      const hist = getHistory();
      for (const r of runs) {
        if (!hist.find(h => h.run_id === r.run_id)) {
          hist.push(r);
        } else {
          const idx = hist.findIndex(h => h.run_id === r.run_id);
          hist[idx] = {...hist[idx], ...r};
        }
      }
      hist.sort((a,b) => (b.created_at||0) - (a.created_at||0));
      if (hist.length > 50) hist.length = 50;
      localStorage.setItem('hm_runs', JSON.stringify(hist));
      renderHistory();
    }
  } catch(e) {}
})();
</script>
</body>
</html>
"""


async def _read_upload_bytes_limited(
    upload: UploadFile,
    *,
    max_bytes: int,
    chunk_size: int = 1024 * 1024,
) -> bytes:
    """Read upload content in chunks and stop once the byte cap is exceeded."""
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(
                f"Archive too large ({total} bytes). Max: {max_bytes} bytes."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_extract_tar(
    archive_bytes: bytes,
    extract_to: str,
    *,
    max_members: int = MAX_UPLOAD_TAR_MEMBERS,
    max_member_bytes: int = MAX_UPLOAD_TAR_MEMBER_BYTES,
    max_total_bytes: int = MAX_UPLOAD_TAR_TOTAL_BYTES,
) -> None:
    """Extract a tar archive while rejecting path traversal and link entries."""
    import io

    base = Path(extract_to).resolve()
    member_count = 0
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as tar:
        for member in tar.getmembers():
            if member.type in _IGNORED_TAR_TYPES:
                continue

            member_count += 1
            if member_count > max_members:
                raise ValueError(
                    f"Archive has too many entries ({member_count} > {max_members})"
                )

            target = (base / member.name).resolve()
            if target != base and base not in target.parents:
                raise ValueError(f"Invalid archive member path: {member.name}")

            if member.issym() or member.islnk():
                raise ValueError(f"Symlink entries are not allowed: {member.name}")

            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue

            if not member.isfile():
                raise ValueError(f"Unsupported archive member: {member.name}")

            member_size = int(member.size or 0)
            if member_size < 0:
                raise ValueError(f"Invalid archive member size: {member.name}")
            if member_size > max_member_bytes:
                raise ValueError(
                    f"Archive member too large ({member.name}: {member_size} bytes)"
                )
            total_bytes += member_size
            if total_bytes > max_total_bytes:
                raise ValueError(
                    f"Archive expands beyond limit ({total_bytes} > {max_total_bytes})"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                raise ValueError(f"Invalid archive member: {member.name}")
            with src, open(target, "wb") as dst:
                remaining = member_size
                while remaining > 0:
                    chunk = src.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError(
                            f"Unexpected end of archive while extracting {member.name}"
                        )
                    dst.write(chunk)
                    remaining -= len(chunk)

            file_mode = member.mode & 0o777
            os.chmod(target, file_mode or 0o644)


def _read_extracted_files(tmpdir: str) -> dict[str, str]:
    """Read all extracted source files from a directory as {path: content}."""
    files: dict[str, str] = {}
    base = Path(tmpdir)
    for fpath in sorted(base.rglob("*")):
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(base))
        # Skip hidden files and __pycache__
        if any(part.startswith(".") or part == "__pycache__" for part in rel.split("/")):
            continue
        try:
            files[rel] = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
    return files


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        hm = Hivemind(settings)
        app.state.hivemind = hm
        yield
        await hm.close()

    app = FastAPI(title="Hivemind Core", version=APP_VERSION, lifespan=lifespan)

    cors_origins = [
        origin.strip()
        for origin in (settings.cors_allow_origins or "").split(",")
        if origin.strip()
    ]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def get_hivemind(request: Request) -> Hivemind:
        return request.app.state.hivemind

    async def check_auth(request: Request):
        if not settings.api_key:
            return
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Unauthorized")
        token = auth.removeprefix("Bearer ").strip()
        if not secrets.compare_digest(token, settings.api_key):
            raise HTTPException(status_code=401, detail="Unauthorized")

    # ── Pipeline endpoints ──

    @app.post(
        "/v1/store",
        response_model=StoreResponse,
        dependencies=[Depends(check_auth)],
    )
    async def store(req: StoreRequest, hm: Hivemind = Depends(get_hivemind)):
        try:
            return await hm.pipeline.run_store(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post(
        "/v1/query",
        response_model=QueryResponse,
        dependencies=[Depends(check_auth)],
    )
    async def query(req: QueryRequest, hm: Hivemind = Depends(get_hivemind)):
        try:
            return await hm.pipeline.run_query(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── Async query (submit + poll) ──
    # For deployments behind reverse proxies with short timeouts (e.g. Phala 60s).

    _pending_queries: dict[str, dict] = {}

    @app.post("/v1/query/submit", dependencies=[Depends(check_auth)])
    async def submit_query(req: QueryRequest, hm: Hivemind = Depends(get_hivemind)):
        """Submit a query for async processing. Returns a run_id to poll."""
        run_id = uuid4().hex[:12]
        _pending_queries[run_id] = {"status": "running", "result": None, "error": None}

        async def _run():
            try:
                result = await hm.pipeline.run_query(req)
                _pending_queries[run_id] = {
                    "status": "completed",
                    "result": result.model_dump(),
                    "error": None,
                }
            except Exception as e:
                _pending_queries[run_id] = {
                    "status": "failed",
                    "result": None,
                    "error": str(e),
                }

        asyncio.create_task(_run())
        return {"run_id": run_id, "status": "running"}

    @app.get("/v1/query/runs/{run_id}", dependencies=[Depends(check_auth)])
    async def get_query_status(run_id: str):
        """Poll the status of an async query."""
        entry = _pending_queries.get(run_id)
        if not entry:
            raise HTTPException(404, "Query run not found")
        return {"run_id": run_id, **entry}

    @app.post(
        "/v1/index",
        response_model=IndexResponse,
        dependencies=[Depends(check_auth)],
    )
    async def index(req: IndexRequest, hm: Hivemind = Depends(get_hivemind)):
        try:
            return await hm.pipeline.run_index(req)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ── Admin schema endpoint ──

    @app.get(
        "/v1/admin/schema",
        dependencies=[Depends(check_auth)],
    )
    async def get_schema(hm: Hivemind = Depends(get_hivemind)):
        schema = await asyncio.to_thread(hm.db.get_schema)
        return {"schema": schema}

    # ── Agent CRUD ──

    from .sandbox.models import AgentConfig, AgentCreateRequest

    @app.post("/v1/agents", dependencies=[Depends(check_auth)])
    async def register_agent(
        req: AgentCreateRequest,
        hm: Hivemind = Depends(get_hivemind),
    ):
        from .sandbox.backend import _create_runner

        sandbox_settings = build_sandbox_settings(settings)
        runner = _create_runner(sandbox_settings)
        try:
            if not runner.image_exists(req.image):
                raise HTTPException(
                    status_code=400,
                    detail=f"Image not found: {req.image}",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Image preflight failed for %s: %s", req.image, e)
            raise HTTPException(
                status_code=503,
                detail="Container backend unavailable for image validation",
            )

        agent_id = uuid4().hex[:12]
        config = AgentConfig(
            agent_id=agent_id,
            name=req.name,
            description=req.description,
            image=req.image,
            entrypoint=req.entrypoint,
            memory_mb=min(req.memory_mb, settings.container_memory_mb),
            max_llm_calls=req.max_llm_calls,
            max_tokens=req.max_tokens,
            timeout_seconds=req.timeout_seconds,
        )
        await asyncio.to_thread(hm.agent_store.create, config)

        # Extract source files from image (non-fatal, no-op for Phala)
        file_count = 0
        try:
            files = await runner.extract_image_files_async(config.image)
            await asyncio.to_thread(hm.agent_store.save_files, agent_id, files)
            file_count = len(files)
        except Exception as e:
            logger.warning("Failed to extract files from %s: %s", config.image, e)

        return {
            "agent_id": agent_id,
            "name": req.name,
            "files_extracted": file_count,
        }

    @app.get("/v1/agents", dependencies=[Depends(check_auth)])
    async def list_agents(hm: Hivemind = Depends(get_hivemind)):
        agents = await asyncio.to_thread(hm.agent_store.list_agents)
        return [a.model_dump() for a in agents]

    @app.get("/v1/agents/{agent_id}", dependencies=[Depends(check_auth)])
    async def get_agent(
        agent_id: str, hm: Hivemind = Depends(get_hivemind)
    ):
        agent = await asyncio.to_thread(hm.agent_store.get, agent_id)
        if not agent:
            raise HTTPException(404, "Agent not found")
        return agent.model_dump()

    @app.delete("/v1/agents/{agent_id}", dependencies=[Depends(check_auth)])
    async def delete_agent(
        agent_id: str, hm: Hivemind = Depends(get_hivemind)
    ):
        if not await asyncio.to_thread(hm.agent_store.delete, agent_id):
            raise HTTPException(404, "Agent not found")
        return {"status": "ok"}

    # ── Agent upload ──

    @app.post("/v1/agents/upload", dependencies=[Depends(check_auth)])
    async def upload_agent(
        name: str = Form(...),
        archive: UploadFile = File(...),
        description: str = Form(""),
        entrypoint: str | None = Form(None),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        hm: Hivemind = Depends(get_hivemind),
    ):
        try:
            content = await _read_upload_bytes_limited(
                archive,
                max_bytes=MAX_UPLOAD_SIZE,
            )
        except ValueError as e:
            raise HTTPException(
                status_code=400,
                detail=str(e),
            )

        tmpdir = tempfile.mkdtemp(prefix="hivemind-upload-")
        try:
            try:
                _safe_extract_tar(content, tmpdir)
            except (tarfile.TarError, ValueError) as e:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid archive: {e}",
                )
            except Exception:
                logger.exception("Unexpected archive extraction failure")
                raise HTTPException(
                    status_code=500,
                    detail="Archive extraction failed",
                )

            agent_id = uuid4().hex[:12]

            sandbox_settings = build_sandbox_settings(settings)
            runner = _create_runner(sandbox_settings)

            image_tag = f"hivemind-agent-{agent_id}:latest"
            try:
                await runner.build_image_async(tmpdir, image_tag)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            except Exception:
                logger.exception("Image build failed for uploaded agent")
                raise HTTPException(
                    status_code=500,
                    detail="Image build failed",
                )

            try:
                config = AgentConfig(
                    agent_id=agent_id,
                    name=name,
                    description=description,
                    image=image_tag,
                    entrypoint=entrypoint,
                    memory_mb=min(memory_mb, settings.container_memory_mb),
                    max_llm_calls=max_llm_calls,
                    max_tokens=max_tokens,
                    timeout_seconds=timeout_seconds,
                )
            except ValidationError as e:
                raise HTTPException(
                    status_code=422,
                    detail=e.errors(),
                )
            await asyncio.to_thread(hm.agent_store.create, config)

            # Save source files to DB
            file_count = 0
            try:
                files = await runner.extract_image_files_async(image_tag)
                await asyncio.to_thread(
                    hm.agent_store.save_files, agent_id, files
                )
                file_count = len(files)
            except Exception as e:
                logger.warning(
                    "Failed to save agent files for %s: %s", agent_id, e
                )

            return {
                "agent_id": agent_id,
                "name": name,
                "files_extracted": file_count,
            }
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    # ── Query agent submit + run tracking ──

    @app.post("/v1/query-agents/submit", dependencies=[Depends(check_auth)])
    async def submit_query_agent(
        name: str = Form(...),
        archive: UploadFile = File(...),
        prompt: str = Form(""),
        description: str = Form(""),
        entrypoint: str | None = Form(None),
        memory_mb: Annotated[int, Form(ge=16)] = 256,
        max_llm_calls: Annotated[int, Form(ge=1)] = 20,
        max_tokens: Annotated[int, Form(ge=1)] = 100_000,
        timeout_seconds: Annotated[int, Form(ge=1)] = 120,
        scope_agent_id: str | None = Form(None),
        mediator_agent_id: str | None = Form(None),
        hm: Hivemind = Depends(get_hivemind),
    ):
        """Upload query agent source, create a run record, and kick off execution."""
        try:
            content = await _read_upload_bytes_limited(
                archive, max_bytes=MAX_UPLOAD_SIZE,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        tmpdir = tempfile.mkdtemp(prefix="hivemind-upload-")
        try:
            _safe_extract_tar(content, tmpdir)
        except (tarfile.TarError, ValueError) as e:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Invalid archive: {e}")

        # Create run record immediately, return fast
        agent_id = uuid4().hex[:12]
        run_id = uuid4().hex[:12]
        await asyncio.to_thread(hm.run_store.create, run_id, agent_id)

        # Everything else runs in background
        asyncio.create_task(
            _build_and_run(
                hm=hm,
                settings=settings,
                tmpdir=tmpdir,
                agent_id=agent_id,
                run_id=run_id,
                name=name,
                description=description,
                entrypoint=entrypoint,
                memory_mb=memory_mb,
                max_llm_calls=max_llm_calls,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
                prompt=prompt,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=mediator_agent_id,
            )
        )

        return {
            "run_id": run_id,
            "agent_id": agent_id,
            "status": "pending",
        }

    async def _build_and_run(
        hm: Hivemind,
        settings: Settings,
        tmpdir: str,
        agent_id: str,
        run_id: str,
        name: str,
        description: str,
        entrypoint: str | None,
        memory_mb: int,
        max_llm_calls: int,
        max_tokens: int,
        timeout_seconds: int,
        prompt: str,
        scope_agent_id: str | None,
        mediator_agent_id: str | None,
    ) -> None:
        """Background task: build image, register agent, run pipeline."""
        from .sandbox.backend import _create_runner

        try:
            # -- Build Docker image --
            import time as _time

            build_t0 = _time.time()
            await asyncio.to_thread(
                hm.run_store.update_stage, run_id, "build", started_at=build_t0,
            )

            sandbox_settings = build_sandbox_settings(settings)
            runner = _create_runner(sandbox_settings)
            image_tag = f"hivemind-agent-{agent_id}:latest"

            try:
                await runner.build_image_async(tmpdir, image_tag)
            except Exception as e:
                logger.exception("Image build failed for agent %s", agent_id)
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=f"Image build failed: {e}",
                )
                return
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)
                await asyncio.to_thread(
                    hm.run_store.update_stage, run_id, "build",
                    ended_at=_time.time(),
                )

            # -- Register agent --
            config = AgentConfig(
                agent_id=agent_id,
                name=name,
                description=description,
                image=image_tag,
                entrypoint=entrypoint,
                memory_mb=min(memory_mb, settings.container_memory_mb),
                max_llm_calls=max_llm_calls,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )
            await asyncio.to_thread(hm.agent_store.create, config)

            try:
                files = await runner.extract_image_files_async(image_tag)
                await asyncio.to_thread(hm.agent_store.save_files, agent_id, files)
            except Exception as e:
                logger.warning("Failed to save agent files for %s: %s", agent_id, e)

            # -- Run pipeline --
            await hm.pipeline.run_query_agent_tracked(
                agent_id=agent_id,
                run_id=run_id,
                run_store=hm.run_store,
                s3_uploader=hm.s3_uploader,
                prompt=prompt,
                scope_agent_id=scope_agent_id,
                mediator_agent_id=mediator_agent_id,
                max_tokens=max_tokens,
            )

        except Exception as e:
            logger.error("Background build+run %s failed: %s", run_id, e)
            try:
                await asyncio.to_thread(
                    hm.run_store.update_status, run_id, "failed",
                    error=str(e)[:500],
                )
            except Exception:
                pass

    @app.get("/v1/query-agents/runs/{run_id}", dependencies=[Depends(check_auth)])
    async def get_query_run(
        run_id: str, hm: Hivemind = Depends(get_hivemind)
    ):
        """Get the status and result of a query agent run."""
        run = await asyncio.to_thread(hm.run_store.get, run_id)
        if not run:
            raise HTTPException(404, "Run not found")
        run["download_url"] = None
        if run.get("s3_url") and hm.s3_uploader:
            run["download_url"] = await asyncio.to_thread(
                hm.s3_uploader.presign_url, run["s3_url"]
            )
        return run

    # ── List recent runs ──

    @app.get("/v1/query-agents/runs", dependencies=[Depends(check_auth)])
    async def list_query_runs(
        limit: int = 20, hm: Hivemind = Depends(get_hivemind)
    ):
        """List recent query agent runs."""
        return await asyncio.to_thread(hm.run_store.list_recent, min(limit, 100))

    # ── Health ──

    @app.get("/v1/health", response_model=HealthResponse)
    async def health(hm: Hivemind = Depends(get_hivemind)):
        return await asyncio.to_thread(hm.health)

    # ── Web UI ──

    @app.get("/", response_class=HTMLResponse)
    async def ui_page():
        return _UI_HTML

    return app


class _LazyApp:
    """ASGI wrapper that delays Settings/.env loading until first request."""

    def __init__(self):
        self._app: FastAPI | None = None
        self._lock = threading.Lock()

    def _get_app(self) -> FastAPI:
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = create_app()
        return self._app

    async def __call__(self, scope, receive, send):
        await self._get_app()(scope, receive, send)


app = _LazyApp()


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
