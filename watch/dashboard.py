"""Hivemind autoresearch live dashboard.

Serves a single-page UI + JSON APIs that expose bench + iter state.
Binds 0.0.0.0:9999 by default; requires login password (no username).

Usage:
    WATCH_PASSWORD=... PYTHONUNBUFFERED=1 .venv/bin/python -m watch.dashboard
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

import uvicorn
from fastapi import Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

REPO = Path(__file__).resolve().parent.parent
RESULTS_TSV = REPO / "autoresearch" / "results.tsv"
BENCH_RESULTS_DIR = REPO / "bench" / "results"
SERVER_LOG = Path("/tmp/server.log")

PASSWORD = os.environ.get("WATCH_PASSWORD") or ""  # MUST be set via env; empty → all logins rejected
COOKIE_NAME = "watch_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
# Issued tokens are kept in-memory; reboot invalidates sessions.
_VALID_TOKENS: set[str] = set()

app = FastAPI(title="hivemind-watch")


def _check_auth(token: str | None) -> bool:
    if not token:
        return False
    for t in _VALID_TOKENS:
        if hmac.compare_digest(t, token):
            return True
    return False


@app.middleware("http")
async def require_auth(request: Request, call_next):
    # Allow login endpoints unconditionally.
    if request.url.path in {"/login", "/logout"} or request.url.path.startswith("/static/"):
        return await call_next(request)
    token = request.cookies.get(COOKIE_NAME)
    if _check_auth(token):
        return await call_next(request)
    # JSON endpoints get 401; HTML navigates to /login.
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse(url="/login", status_code=302)


def _tail(path: Path, n: int = 50) -> list[str]:
    if not path.exists():
        return []
    try:
        data = path.read_bytes()[-256_000:]
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _latest_bench_log() -> Path | None:
    logs = sorted(Path("/tmp").glob("iter*_bench.log"), key=lambda p: p.stat().st_mtime)
    return logs[-1] if logs else None


def _ps_bench() -> dict | None:
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,etime,pcpu,pmem,args"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if "bench.cli" in line and "grep" not in line:
            parts = line.split(None, 4)
            if len(parts) == 5:
                return {"pid": parts[0], "elapsed": parts[1], "cpu": parts[2], "mem": parts[3], "cmd": parts[4]}
    return None


def _docker_ps() -> list[dict]:
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except Exception:
        return []
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            rows.append({"name": parts[0], "image": parts[1], "status": parts[2], "ports": parts[3]})
    return rows


def _parse_bench_log(path: Path) -> dict:
    """Extract scenario/round/attack progress from the tail of a bench log."""
    state = {
        "scenario_name": None,
        "scenario_num": None,
        "scenario_total": None,
        "round_num": None,
        "round_total": None,
        "attack_num": None,
        "attack_total": None,
        "attack_text": None,
        "last_verdict": None,
        "per_scenario": [],
    }
    if not path.exists():
        return state
    try:
        text = path.read_text("utf-8", errors="replace")
    except Exception:
        return state
    for m in re.finditer(r"Scenario (\d+)/(\d+): (\S+)", text):
        state["scenario_num"] = int(m.group(1))
        state["scenario_total"] = int(m.group(2))
        state["scenario_name"] = m.group(3)
    for m in re.finditer(r"Round (\d+)/(\d+)", text):
        state["round_num"] = int(m.group(1))
        state["round_total"] = int(m.group(2))
    for m in re.finditer(r"\[(\d+)/(\d+)\]\s+(.+)", text):
        state["attack_num"] = int(m.group(1))
        state["attack_total"] = int(m.group(2))
        state["attack_text"] = m.group(3).strip()
    for m in re.finditer(r"→ (SAFE|LEAKED)(\s+\[useful\])?\s+\((\d+)ms\)", text):
        state["last_verdict"] = {
            "verdict": m.group(1),
            "useful": bool(m.group(2)),
            "ms": int(m.group(3)),
        }
    for m in re.finditer(
        r"Round \d+ — Defense:\s+(\d+)% \| Utility:\s+(\d+)% \| Grade:\s+(\S+)", text
    ):
        state["per_scenario"].append(
            {"defense": int(m.group(1)), "utility": int(m.group(2)), "grade": m.group(3)}
        )
    overall = re.search(
        r"OVERALL\s+(\d+)%\s+(\d+)%\s+(\d+)%\s+(\S+)", text
    )
    if overall:
        state["overall"] = {
            "defense": int(overall.group(1)),
            "utility": int(overall.group(2)),
            "combined": int(overall.group(3)),
            "grade": overall.group(4),
        }
    return state


@app.get("/api/live")
def live() -> dict:
    bench_log = _latest_bench_log()
    return {
        "timestamp": time.time(),
        "bench_proc": _ps_bench(),
        "docker": _docker_ps(),
        "bench_log_path": str(bench_log) if bench_log else None,
        "bench_log_tail": _tail(bench_log, 80) if bench_log else [],
        "server_log_tail": _tail(SERVER_LOG, 30),
        "progress": _parse_bench_log(bench_log) if bench_log else {},
    }


@app.get("/api/iters")
def iters() -> dict:
    if not RESULTS_TSV.exists():
        return {"header": [], "rows": []}
    lines = RESULTS_TSV.read_text("utf-8").splitlines()
    if not lines:
        return {"header": [], "rows": []}
    header = lines[0].split("\t")
    rows = []
    for ln in lines[1:]:
        parts = ln.split("\t")
        if len(parts) == len(header):
            rows.append(dict(zip(header, parts)))
    return {"header": header, "rows": rows}


@app.get("/api/latest_result")
def latest_result() -> dict:
    if not BENCH_RESULTS_DIR.exists():
        return {}
    files = sorted(BENCH_RESULTS_DIR.glob("gan-*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        return {}
    try:
        data = json.loads(files[-1].read_text("utf-8"))
        return {
            "file": files[-1].name,
            "timestamp": data.get("timestamp"),
            "overall": data.get("overall_scores", {}),
            "scenarios": [
                {
                    "scenario": s.get("scenario"),
                    "policy": s.get("policy"),
                    "rounds": [
                        {
                            "round": r.get("round"),
                            "attack_count": r.get("attack_count"),
                            "defense_rate": r.get("defense_rate"),
                            "utility_score": r.get("utility_score"),
                            "grade": r.get("grade"),
                        }
                        for r in s.get("rounds", [])
                    ],
                }
                for s in data.get("scenarios", [])
            ],
        }
    except Exception as exc:
        return {"error": str(exc)}


HTML = """<!doctype html>
<html lang=en>
<head>
<meta charset=utf-8>
<title>hivemind · live</title>
<style>
 :root { color-scheme: dark; }
 * { box-sizing: border-box; }
 body { margin: 0; font: 13px/1.45 ui-monospace, Menlo, Consolas, monospace;
        background: #0e0f13; color: #d3d7de; }
 header { padding: 14px 22px; border-bottom: 1px solid #24262d;
          display: flex; align-items: center; gap: 16px; }
 header h1 { margin: 0; font-size: 14px; letter-spacing: .5px;
             color: #fff; font-weight: 600; }
 #dot { width: 9px; height: 9px; border-radius: 50%;
        background: #4a4e56; display: inline-block; }
 #dot.live { background: #4ad47a; box-shadow: 0 0 8px #4ad47a80; }
 #dot.done { background: #5f7cff; }
 #status { color: #a8aebc; }
 main { padding: 16px 22px; display: grid; gap: 16px;
        grid-template-columns: 1.1fr 1fr; align-items: start; }
 section { background: #151820; border: 1px solid #24262d;
           border-radius: 8px; padding: 12px 16px; }
 section h2 { margin: 0 0 10px; font-size: 11px;
              text-transform: uppercase; letter-spacing: 1px;
              color: #7f8899; font-weight: 600; }
 table { width: 100%; border-collapse: collapse; font-size: 12px; }
 th, td { padding: 4px 6px; text-align: left; border-bottom: 1px solid #1c1f27; }
 th { color: #7f8899; font-weight: 500; font-size: 10px;
      text-transform: uppercase; letter-spacing: .5px; }
 td.num { text-align: right; font-variant-numeric: tabular-nums; }
 .grade-A { color: #4ad47a; font-weight: 600; }
 .grade-B { color: #5fe1c5; font-weight: 600; }
 .grade-C { color: #e1c158; font-weight: 600; }
 .grade-D { color: #e69758; font-weight: 600; }
 .grade-F { color: #e65858; font-weight: 600; }
 pre { margin: 0; font-size: 11px; max-height: 380px;
       overflow: auto; white-space: pre; color: #b8bdca; }
 .attack { color: #fff; font-size: 13px; margin: 6px 0; }
 .attack small { color: #7f8899; font-weight: normal; }
 .verdict-SAFE { color: #4ad47a; }
 .verdict-LEAKED { color: #e65858; }
 .muted { color: #7f8899; font-size: 11px; }
 .bar { height: 6px; background: #222; border-radius: 3px; overflow: hidden;
        margin-top: 4px; }
 .bar > span { display: block; height: 100%; background: #5fe1c5; }
 .scenario-list li { list-style: none; padding: 3px 0;
                     border-bottom: 1px solid #1c1f27; font-size: 12px;
                     display: flex; justify-content: space-between; }
 .scenario-list { margin: 0; padding: 0; }
 .scenario-list li.current { color: #fff; font-weight: 600; }
 .scenario-list li.current::before { content: "▶ "; color: #4ad47a; }
 footer { padding: 10px 22px; color: #555a65; font-size: 10px;
          border-top: 1px solid #24262d; display: flex;
          justify-content: space-between; }
</style>
</head>
<body>
<header>
 <span id=dot></span>
 <h1>hivemind · autoresearch</h1>
 <span id=status>loading…</span>
</header>
<main>
 <section>
  <h2>live bench</h2>
  <div id=progress></div>
  <div id=current-attack class=attack></div>
  <ul id=scenario-progress class=scenario-list></ul>
 </section>
 <section>
  <h2>last completed run</h2>
  <div id=last-run></div>
 </section>
 <section style="grid-column: 1 / -1">
  <h2>iter history <span class=muted id=iters-meta></span></h2>
  <div style="max-height:360px;overflow:auto">
   <table id=iters-table><thead></thead><tbody></tbody></table>
  </div>
 </section>
 <section>
  <h2>bench log <span class=muted id=bench-log-path></span></h2>
  <pre id=bench-log></pre>
 </section>
 <section>
  <h2>server log</h2>
  <pre id=server-log></pre>
 </section>
 <section style="grid-column: 1 / -1">
  <h2>containers</h2>
  <table id=docker><thead></thead><tbody></tbody></table>
 </section>
</main>
<footer>
 <span id=last-update></span>
 <span>refresh: 3s · <a href=/api/live style="color:#5f7cff">raw json</a></span>
</footer>
<script>
const SCENARIOS = [
 "pii_redaction","aggregation_only","topic_filtering",
 "temporal_scoping","content_sanitization","prompt_injection",
];

function gradeClass(g){ return g ? ("grade-" + (g[0] || "")) : ""; }

async function refreshLive(){
 const r = await fetch("/api/live").then(x=>x.json());
 const dot = document.getElementById("dot");
 const status = document.getElementById("status");
 const p = r.progress || {};
 if (r.bench_proc){
  dot.className = "live";
  const s = p.scenario_num ? `scenario ${p.scenario_num}/${p.scenario_total} · ${p.scenario_name}` : "starting…";
  status.textContent = `running · ${s} · pid ${r.bench_proc.pid} · ${r.bench_proc.elapsed}`;
 } else if (p.overall){
  dot.className = "done";
  status.textContent = `idle · last run ended grade ${p.overall.grade} ${p.overall.combined}%`;
 } else {
  dot.className = "";
  status.textContent = "idle";
 }
 // current attack
 const ca = document.getElementById("current-attack");
 if (p.attack_num){
  const v = p.last_verdict;
  let vtxt = "";
  if (v){
   const cls = "verdict-" + v.verdict;
   vtxt = ` <span class=${cls}>→ ${v.verdict}${v.useful?" [useful]":""}</span> <small>${(v.ms/1000).toFixed(1)}s</small>`;
  }
  ca.innerHTML = `<small>attack ${p.attack_num}/${p.attack_total}:</small> ${escapeHtml(p.attack_text||"")}${vtxt}`;
 } else {
  ca.textContent = "";
 }
 // scenario progress list
 const sp = document.getElementById("scenario-progress");
 sp.innerHTML = "";
 for (let i=0; i<SCENARIOS.length; i++){
  const name = SCENARIOS[i];
  const r1 = (p.per_scenario||[])[i];
  const li = document.createElement("li");
  const current = p.scenario_num === (i+1) && !r1;
  if (current) li.className = "current";
  let right = "";
  if (r1){
   right = `<span><span class=${gradeClass(r1.grade)}>${r1.grade}</span> <small class=muted>d${r1.defense} u${r1.utility}</small></span>`;
  } else if (current){
   right = "<small class=muted>running…</small>";
  } else {
   right = "<small class=muted>—</small>";
  }
  li.innerHTML = `<span>${i+1}. ${name}</span>${right}`;
  sp.appendChild(li);
 }
 // overall progress bar
 const prog = document.getElementById("progress");
 if (p.scenario_num){
  const done = p.per_scenario?.length||0;
  const pct = Math.round((done / (p.scenario_total||6)) * 100);
  prog.innerHTML = `<div class=muted>${done}/${p.scenario_total||6} scenarios complete</div><div class=bar><span style="width:${pct}%"></span></div>`;
 } else {
  prog.innerHTML = "<div class=muted>no bench running</div>";
 }
 // logs
 document.getElementById("bench-log").textContent = (r.bench_log_tail||[]).join("\\n");
 document.getElementById("server-log").textContent = (r.server_log_tail||[]).join("\\n");
 document.getElementById("bench-log-path").textContent = r.bench_log_path || "";
 // docker
 const dTbl = document.getElementById("docker");
 dTbl.querySelector("thead").innerHTML = "<tr><th>name</th><th>image</th><th>status</th><th>ports</th></tr>";
 dTbl.querySelector("tbody").innerHTML = (r.docker||[]).map(c=>
  `<tr><td>${c.name}</td><td>${c.image}</td><td>${c.status}</td><td>${c.ports||""}</td></tr>`
 ).join("");
 // auto-scroll logs to bottom
 for (const id of ["bench-log","server-log"]){
  const el = document.getElementById(id); el.scrollTop = el.scrollHeight;
 }
 document.getElementById("last-update").textContent = "updated " + new Date().toLocaleTimeString();
}

async function refreshIters(){
 const r = await fetch("/api/iters").then(x=>x.json());
 const hdr = r.header || [];
 const show = ["timestamp","label","grade","defense","utility","notes"];
 const keep = show.filter(k => hdr.includes(k));
 const t = document.getElementById("iters-table");
 t.querySelector("thead").innerHTML = "<tr>" + keep.map(h=>`<th>${h}</th>`).join("") + "</tr>";
 const rows = (r.rows||[]).slice().reverse();
 t.querySelector("tbody").innerHTML = rows.map(row => {
  return "<tr>" + keep.map(k=>{
   let v = row[k] || "";
   if (k === "notes" && v.length > 140) v = v.slice(0,140) + "…";
   if (k === "grade") return `<td class=${gradeClass(v)}>${v}</td>`;
   return `<td>${escapeHtml(v)}</td>`;
  }).join("") + "</tr>";
 }).join("");
 document.getElementById("iters-meta").textContent = `(${rows.length} iters)`;
}

async function refreshLastRun(){
 const r = await fetch("/api/latest_result").then(x=>x.json());
 const el = document.getElementById("last-run");
 if (!r || !r.overall){
  el.innerHTML = "<div class=muted>no completed runs</div>";
  return;
 }
 const o = r.overall;
 let html = `<div style="font-size:14px">
  <span class=${gradeClass(o.grade)}>${o.grade||""}</span>
  <span style="color:#fff">${o.combined||0}%</span>
  <span class=muted>def ${o.defense_rate?Math.round(o.defense_rate*100):0}%</span>
  <span class=muted>util ${o.utility_score?Math.round(o.utility_score*100):0}%</span>
  <span class=muted>${o.defended||0}/${o.total||0}</span>
 </div><small class=muted>${r.file||""}</small>
 <table style="margin-top:8px"><thead><tr><th>scenario</th><th>rounds</th></tr></thead><tbody>`;
 for (const s of (r.scenarios||[])){
  const grades = (s.rounds||[]).map(rd=>`<span class=${gradeClass(rd.grade)}>${rd.grade||"-"}</span>`).join(" ");
  html += `<tr><td>${s.scenario}</td><td>${grades}</td></tr>`;
 }
 html += "</tbody></table>";
 el.innerHTML = html;
}

function escapeHtml(s){
 return (s||"").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\\"":"&quot;","'":"&#39;"}[c]));
}

refreshLive(); refreshIters(); refreshLastRun();
setInterval(refreshLive, 3000);
setInterval(refreshIters, 30000);
setInterval(refreshLastRun, 10000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(HTML)


LOGIN_HTML = """<!doctype html>
<html lang=en><head><meta charset=utf-8><title>hivemind · login</title>
<style>
 :root { color-scheme: dark; }
 body { margin: 0; font: 14px/1.5 ui-monospace, Menlo, monospace;
        background: #0e0f13; color: #d3d7de;
        display: flex; align-items: center; justify-content: center;
        height: 100vh; }
 form { background: #151820; border: 1px solid #24262d; border-radius: 8px;
        padding: 28px 32px; min-width: 280px;
        box-shadow: 0 4px 24px rgba(0,0,0,.4); }
 h1 { margin: 0 0 18px; font-size: 13px; letter-spacing: 1px;
      text-transform: uppercase; color: #7f8899; font-weight: 600; }
 input[type=password] { width: 100%; padding: 10px 12px;
        background: #0e0f13; border: 1px solid #24262d; border-radius: 4px;
        color: #fff; font: inherit; }
 input[type=password]:focus { outline: none; border-color: #5f7cff; }
 button { width: 100%; margin-top: 12px; padding: 10px;
          background: #5f7cff; border: none; border-radius: 4px;
          color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
 button:hover { background: #7490ff; }
 .err { margin-top: 10px; color: #e65858; font-size: 12px; min-height: 1em; }
</style></head><body>
<form method=post action=/login>
 <h1>hivemind · autoresearch</h1>
 <input type=password name=password placeholder=password autofocus required>
 <button type=submit>enter</button>
 <div class=err>{error}</div>
</form></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    # If already authenticated, bounce to dashboard.
    if _check_auth(request.cookies.get(COOKIE_NAME)):
        return HTMLResponse(
            '<meta http-equiv="refresh" content="0; url=/">', status_code=200
        )
    return HTMLResponse(LOGIN_HTML.replace("{error}", ""))


@app.post("/login")
def login_submit(password: str = Form(...)) -> Response:
    if not PASSWORD or not hmac.compare_digest(password, PASSWORD):
        # Small delay to dampen brute-force.
        time.sleep(0.5)
        return HTMLResponse(
            LOGIN_HTML.replace("{error}", "wrong password"),
            status_code=401,
        )
    token = secrets.token_urlsafe(32)
    _VALID_TOKENS.add(token)
    resp = RedirectResponse(url="/", status_code=302)
    resp.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.get("/logout")
def logout(request: Request) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        _VALID_TOKENS.discard(token)
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp


def main() -> None:
    host = os.environ.get("WATCH_HOST", "0.0.0.0")
    port = int(os.environ.get("WATCH_PORT", "9999"))
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
