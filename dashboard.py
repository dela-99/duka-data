"""
dashboard.py — Lightweight local control panel for the Dukascopy downloader
============================================================================
A single-file, zero-dependency (stdlib only) web UI to TRIGGER downloads and
WATCH live progress. No Flask/Node/build step — just:

    python3 dashboard.py            # opens http://127.0.0.1:8765 in your browser

Endpoints (all served by Python's built-in http.server):
    GET  /            -> the single-page UI (embedded HTML/CSS/JS)
    GET  /config      -> selectable symbol universe + default dates
    GET  /status      -> contents of .download_status.json (live progress)
    GET  /log?n=200   -> tail of the most recent logs/*.log file
    GET  /files?symbols=A,B -> existing compiled/ledger files + sizes per symbol
    POST /start       -> launch download.py detached in the given mode
                         (download|incremental|repair|fresh); refused if one is running
    POST /clear       -> launch download.py --clear --yes for the given symbols
                         (the UI's backup modal is the confirmation)

Security: binds to 127.0.0.1 ONLY. Because /start executes a subprocess, this
must never be exposed to a network. Symbols/dates are validated before use and
the child is spawned with an argv list (no shell), so there is no shell
injection surface.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE_DIR    = Path(__file__).parent.resolve()
DOWNLOAD_PY = BASE_DIR / "download.py"
LOG_DIR     = BASE_DIR / "logs"
STATUS_FILE = BASE_DIR / ".download_status.json"
COMPILED_DIR = BASE_DIR / "compiled"
LEDGER_DIR   = BASE_DIR / "ledger"
TIMEFRAMES   = ("M5", "H1", "H4", "D1")

# Run mode -> extra argv for download.py. "fresh" passes --yes because the
# detached child has no TTY; the dashboard's backup modal is the confirmation.
MODES: dict[str, list[str]] = {
    "download":    [],
    "incremental": ["--incremental"],
    "repair":      ["--repair"],
    "fresh":       ["--fresh", "--yes"],
}

# Selectable universe shown in the UI. Labels are presentation-only; the symbol
# is what gets passed to download.py (Dukascopy datafeed name).
SYMBOL_GROUPS: dict[str, list[str]] = {
    "FX majors":  ["EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCAD", "NZDUSD"],
    "FX crosses": ["EURGBP", "EURJPY", "GBPJPY", "AUDJPY"],
    "Metals":     ["XAUUSD", "XAGUSD"],
    "Energy":     ["LIGHTCMDUSD", "BRENTCMDUSD"],
    "Indices":    ["USA500IDXUSD", "USA30IDXUSD", "USATECHIDXUSD"],
}
LABELS = {
    "XAUUSD": "Gold", "XAGUSD": "Silver",
    "LIGHTCMDUSD": "WTI crude", "BRENTCMDUSD": "Brent crude",
    "USA500IDXUSD": "S&P 500", "USA30IDXUSD": "Dow 30", "USATECHIDXUSD": "Nasdaq 100",
}
ALL_SYMBOLS = {s for group in SYMBOL_GROUPS.values() for s in group}
SYMBOL_RE = re.compile(r"^[A-Z0-9]{3,20}$")


# ---------------------------------------------------------------------------
# Download process helpers
# ---------------------------------------------------------------------------

def running_download_pids() -> list[int]:
    """PIDs of any live `download.py` process (started by us or anything else)."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "download.py"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return []
    me = os.getpid()
    return [int(p) for p in out.split() if p.strip().isdigit() and int(p) != me]


def read_status() -> dict:
    if not STATUS_FILE.exists():
        return {"state": "idle"}
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {"state": "unknown"}


def tail_log(n: int = 200) -> dict:
    """Last n lines of the most recently modified logs/*.log file."""
    if not LOG_DIR.is_dir():
        return {"file": None, "lines": []}
    logs = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return {"file": None, "lines": []}
    path = logs[0]
    with path.open("rb") as f:
        size = path.stat().st_size
        f.seek(max(0, size - 65536))          # only the tail, not the whole file
        chunk = f.read().decode("utf-8", "replace")
    lines = chunk.splitlines()[-n:]
    return {"file": path.name, "lines": lines}


def _validate_symbols(payload: dict):
    symbols = payload.get("symbols") or []
    if not isinstance(symbols, list) or not symbols:
        return None, "Select at least one symbol."
    symbols = [str(s).upper() for s in symbols]
    bad = [s for s in symbols if not SYMBOL_RE.match(s)]
    if bad:
        return None, f"Invalid symbol(s): {', '.join(bad)}"
    return symbols, None


def _launch(cmd: list, tag: str) -> dict:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"dashboard_run_{ts}.log"
    logf = open(log_path, "ab")
    logf.write(f"[{datetime.datetime.now().isoformat(timespec='seconds')}] launching ({tag}): "
               f"{' '.join(cmd)}\n".encode())
    logf.flush()
    # start_new_session detaches the child so it survives the dashboard exiting.
    proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                            cwd=str(BASE_DIR), start_new_session=True)
    return {"ok": True, "pid": proc.pid, "log": log_path.name, "cmd": " ".join(cmd), "mode": tag}


def start_download(payload: dict) -> dict:
    """Validate payload and launch download.py detached in the requested mode."""
    if running_download_pids():
        return {"ok": False, "error": "A download is already running. Wait for it to finish."}
    symbols, err = _validate_symbols(payload)
    if err:
        return {"ok": False, "error": err}
    mode = str(payload.get("mode") or "download")
    if mode not in MODES:
        return {"ok": False, "error": f"Unknown mode '{mode}'. Use one of: {', '.join(MODES)}."}

    start = str(payload.get("start", "")).strip()
    end = str(payload.get("end", "")).strip()
    date_args: list = []
    if mode == "repair" and not start and not end:
        pass                                    # whole ledger range
    else:
        try:
            datetime.date.fromisoformat(start)
            datetime.date.fromisoformat(end)
        except ValueError:
            return {"ok": False, "error": "start/end must be valid YYYY-MM-DD dates."}
        if start >= end:
            return {"ok": False, "error": "start date must be before end date."}
        date_args = ["--start", start, "--end", end]

    cmd = [sys.executable, "-u", str(DOWNLOAD_PY), "--symbols", *symbols, *date_args, *MODES[mode]]
    return _launch(cmd, mode)


def clear_data(payload: dict) -> dict:
    """Launch download.py --clear --yes for the given symbols (UI already confirmed)."""
    if running_download_pids():
        return {"ok": False, "error": "A download is already running. Wait for it to finish."}
    symbols, err = _validate_symbols(payload)
    if err:
        return {"ok": False, "error": err}
    cmd = [sys.executable, "-u", str(DOWNLOAD_PY), "--clear", "--yes", "--symbols", *symbols]
    return _launch(cmd, "clear")


def list_files(symbols: list) -> dict:
    """Existing compiled CSVs and ledger per symbol with byte sizes."""
    out: dict = {}
    for s in symbols:
        s = str(s).upper()
        if not SYMBOL_RE.match(s):
            raise ValueError(f"invalid symbol {s!r}")
        files = []
        for tf in TIMEFRAMES:
            p = COMPILED_DIR / f"{s}_{tf}.csv"
            if p.exists():
                files.append({"file": f"compiled/{p.name}", "bytes": p.stat().st_size})
        lp = LEDGER_DIR / f"{s}.csv"
        if lp.exists():
            files.append({"file": f"ledger/{lp.name}", "bytes": lp.stat().st_size})
        out[s] = files
    return out


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):           # silence default request logging
        pass

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        route = urlparse(self.path)
        path, qs = route.path, parse_qs(route.query)

        if path == "/":
            self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/config":
            groups = {
                name: [{"symbol": s, "label": LABELS.get(s, s)} for s in syms]
                for name, syms in SYMBOL_GROUPS.items()
            }
            yesterday = datetime.date.today() - datetime.timedelta(days=1)
            self._json({"groups": groups,
                        "default_start": "2006-05-24",
                        "default_end": yesterday.isoformat()})
        elif path == "/status":
            st = read_status()
            st["_running_pids"] = running_download_pids()
            self._json(st)
        elif path == "/log":
            n = int(qs.get("n", ["200"])[0])
            self._json(tail_log(max(1, min(n, 2000))))
        elif path == "/files":
            syms = [s for s in qs.get("symbols", [""])[0].split(",") if s]
            try:
                self._json(list_files(syms))
            except ValueError as e:
                self._json({"error": str(e)}, 400)
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        route = urlparse(self.path)
        handlers = {"/start": start_download, "/clear": clear_data}
        fn = handlers.get(route.path)
        if fn is None:
            self._json({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json({"ok": False, "error": "bad JSON body"}, 400)
            return
        result = fn(payload)
        self._json(result, 200 if result.get("ok") else 409)


# ---------------------------------------------------------------------------
# Single-page UI
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>duka-data dashboard</title>
<style>
  :root { --bg:#0f1419; --panel:#1a2029; --line:#2a3340; --fg:#e6edf3; --mut:#8b98a5;
          --accent:#3b82f6; --ok:#22c55e; --warn:#f59e0b; --err:#ef4444; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:16px 24px; border-bottom:1px solid var(--line); display:flex;
           align-items:center; gap:12px; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  .badge { font-size:12px; padding:2px 10px; border-radius:999px; background:var(--line);
           color:var(--mut); text-transform:uppercase; letter-spacing:.04em; }
  .badge.running { background:#13315c; color:#7cc0ff; }
  .badge.completed { background:#10341f; color:#6ee7a0; }
  .badge.error,.badge.unknown { background:#3a1417; color:#ff9a9a; }
  main { max-width:1080px; margin:0 auto; padding:24px; display:grid;
         grid-template-columns:1fr 1fr; gap:20px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:18px; }
  .panel h2 { font-size:13px; text-transform:uppercase; letter-spacing:.05em;
              color:var(--mut); margin:0 0 14px; }
  .full { grid-column:1 / -1; }
  .grp { margin-bottom:12px; }
  .grp .gh { font-size:12px; color:var(--mut); margin-bottom:4px; display:flex;
             justify-content:space-between; }
  .grp .gh a { color:var(--accent); cursor:pointer; text-decoration:none; font-size:11px; }
  label.chip { display:inline-flex; align-items:center; gap:5px; padding:4px 9px; margin:3px 4px 3px 0;
               border:1px solid var(--line); border-radius:7px; cursor:pointer; font-size:13px; }
  label.chip:hover { border-color:var(--accent); }
  label.chip input { accent-color:var(--accent); }
  .row { display:flex; gap:14px; align-items:end; flex-wrap:wrap; margin-top:10px; }
  .field { display:flex; flex-direction:column; gap:4px; }
  .field span { font-size:11px; color:var(--mut); }
  input[type=date] { background:var(--bg); border:1px solid var(--line); color:var(--fg);
                     border-radius:7px; padding:7px 9px; font-size:13px; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px; padding:9px 18px;
           font-size:14px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .msg { margin-top:10px; font-size:13px; min-height:18px; }
  .msg.ok { color:var(--ok); } .msg.err { color:var(--err); }
  .bar { height:14px; background:var(--bg); border:1px solid var(--line); border-radius:7px;
         overflow:hidden; }
  .bar > div { height:100%; width:0; background:linear-gradient(90deg,#2563eb,#3b82f6);
               transition:width .4s; }
  .stats { display:grid; grid-template-columns:repeat(3,1fr); gap:10px 16px; margin-top:14px; }
  .stat { font-size:13px; } .stat b { display:block; color:var(--mut); font-size:11px;
          text-transform:uppercase; letter-spacing:.04em; font-weight:600; }
  pre#log { background:#0b0e12; border:1px solid var(--line); border-radius:8px; padding:12px;
            font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; color:#b8c4d0;
            max-height:340px; overflow:auto; white-space:pre-wrap; margin:0; }
  .hint { color:var(--mut); font-size:12px; margin-top:6px; }
  .modes { display:flex; gap:6px; flex-wrap:wrap; margin-top:10px; }
  .modes label { display:inline-flex; align-items:center; gap:6px; padding:6px 10px;
                 border:1px solid var(--line); border-radius:8px; cursor:pointer; font-size:13px; }
  .modes label:has(input:checked) { border-color:var(--accent); background:#13233d; }
  .modes .danger:has(input:checked) { border-color:var(--err); background:#3a1417; }
  button.secondary { background:transparent; border:1px solid var(--err); color:var(--err); }
  .badge.warn { background:#3a2a0f; color:#ffd27c; }
  .modal-bg { position:fixed; inset:0; background:rgba(0,0,0,.6); display:none;
              align-items:center; justify-content:center; z-index:10; }
  .modal-bg.show { display:flex; }
  .modal { background:var(--panel); border:1px solid var(--err); border-radius:12px;
           padding:22px; max-width:560px; width:92%; }
  .modal h3 { margin:0 0 10px; color:var(--err); font-size:15px; }
  .modal pre { background:#0b0e12; border:1px solid var(--line); border-radius:8px; padding:10px;
               font:12px/1.4 ui-monospace,Menlo,monospace; max-height:200px; overflow:auto; margin:8px 0; }
  .modal input[type=text] { width:100%; background:var(--bg); border:1px solid var(--line);
               color:var(--fg); border-radius:7px; padding:8px; font-size:14px; margin:8px 0; }
  .modal .actions { display:flex; gap:10px; justify-content:flex-end; }
</style>
</head>
<body>
<header>
  <h1>duka-data</h1>
  <span id="state" class="badge">idle</span>
  <span id="sub" class="hint"></span>
</header>
<main>
  <section class="panel">
    <h2>Start a download</h2>
    <div id="groups"></div>
    <div class="modes" id="modes">
      <label><input type="radio" name="mode" value="download" checked> Download (merge)</label>
      <label><input type="radio" name="mode" value="incremental"> Incremental</label>
      <label><input type="radio" name="mode" value="repair"> Repair only</label>
      <label class="danger"><input type="radio" name="mode" value="fresh"> Re-run from scratch</label>
    </div>
    <div class="row">
      <label class="field"><span>Start</span><input type="date" id="start"></label>
      <label class="field"><span>End</span><input type="date" id="end"></label>
      <button id="go">Start</button>
      <button id="clear" class="secondary">Clear existing data…</button>
    </div>
    <div id="msg" class="msg"></div>
    <div class="hint" id="modehint">Merges new hours into existing data. Refused if a download is already running. Runs detached — safe to close this tab.</div>
  </section>

  <section class="panel">
    <h2>Live progress</h2>
    <div class="bar"><div id="prog"></div></div>
    <div id="progtxt" class="hint" style="margin-top:8px">No active download.</div>
    <div class="stats" id="stats"></div>
  </section>

  <section class="panel full">
    <h2>Log <span id="logname" class="hint"></span></h2>
    <pre id="log">…</pre>
  </section>
</main>

<div class="modal-bg" id="modal">
  <div class="modal">
    <h3 id="mtitle">Delete existing data?</h3>
    <div id="mbody">The following files will be deleted. This cannot be undone — the data can only be recovered by re-downloading it.</div>
    <pre id="mfiles">…</pre>
    <div class="hint">Back up first if you need them, e.g.<br><code id="mbackup">cp -r compiled ledger ~/duka-backup</code></div>
    <input type="text" id="mconfirm" placeholder="Type DELETE to enable the button" autocomplete="off">
    <div class="actions">
      <button id="mcancel" class="secondary">Cancel</button>
      <button id="mok" disabled>Delete</button>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = n => (n==null?'—':Number(n).toLocaleString());
const eta = m => m==null?'—':(m>=60?`${Math.floor(m/60)}h ${Math.round(m%60)}m`:`${Math.round(m)}m`);

async function loadConfig(){
  const c = await (await fetch('/config')).json();
  const wrap = $('groups'); wrap.innerHTML='';
  for(const [name,syms] of Object.entries(c.groups)){
    const g = document.createElement('div'); g.className='grp';
    const def = name.startsWith('FX'); // FX preselected by default
    g.innerHTML = `<div class="gh"><span>${name}</span>
      <span><a data-all>all</a> · <a data-none>none</a></span></div>`;
    const box = document.createElement('div');
    for(const s of syms){
      const l = document.createElement('label'); l.className='chip';
      l.innerHTML = `<input type="checkbox" value="${s.symbol}" ${def?'checked':''}>`+
                    `${s.label}${s.label!==s.symbol?` <span class="hint">${s.symbol}</span>`:''}`;
      box.appendChild(l);
    }
    g.appendChild(box);
    g.querySelector('[data-all]').onclick=()=>box.querySelectorAll('input').forEach(i=>i.checked=true);
    g.querySelector('[data-none]').onclick=()=>box.querySelectorAll('input').forEach(i=>i.checked=false);
    wrap.appendChild(g);
  }
  $('start').value=c.default_start; $('end').value=c.default_end;
}

function chosen(){ return [...document.querySelectorAll('#groups input:checked')].map(i=>i.value); }

const MODE_HINTS = {
  download: 'Merges new hours into existing data.',
  incremental: 'Fetches only days after the last attempted day in the ledger.',
  repair: 'Re-fetches hours that failed or were never attempted. Dates optional (narrow the range).',
  fresh: 'DELETES the selected symbols\' compiled files and ledger, then downloads the range.',
};
const mode = () => document.querySelector('#modes input:checked').value;
document.querySelectorAll('#modes input').forEach(i => i.onchange = () => {
  $('modehint').textContent = MODE_HINTS[mode()] + ' Refused if a download is already running.';
});

function fmtBytes(b){ return b>=1048576 ? (b/1048576).toFixed(1)+' MB' : b>=1024 ? (b/1024).toFixed(1)+' KB' : b+' B'; }

async function confirmDelete(symbols, title){
  const files = await (await fetch('/files?symbols='+encodeURIComponent(symbols.join(',')))).json();
  const lines = [];
  for(const [s, fs] of Object.entries(files)){
    if(!fs.length){ lines.push(`${s}: (no files)`); continue; }
    for(const f of fs) lines.push(`${f.file.padEnd(32)} ${fmtBytes(f.bytes)}`);
  }
  $('mtitle').textContent = title;
  $('mfiles').textContent = lines.join('\n');
  $('mbackup').textContent = `cp -r compiled ledger ~/duka-backup-${new Date().toISOString().slice(0,10)}`;
  $('mconfirm').value=''; $('mok').disabled=true;
  $('modal').classList.add('show'); $('mconfirm').focus();
  return new Promise(resolve => {
    const done = v => { $('modal').classList.remove('show'); resolve(v); };
    $('mconfirm').oninput = () => { $('mok').disabled = $('mconfirm').value.trim() !== 'DELETE'; };
    $('mcancel').onclick = () => done(false);
    $('mok').onclick = () => done(true);
  });
}

async function post(url, body, btn){
  const m=$('msg'); m.className='msg'; m.textContent='Starting…'; btn.disabled=true;
  try{
    const r = await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const j = await r.json();
    if(j.ok){ m.className='msg ok'; m.textContent=`Started ${j.mode} (PID ${j.pid}) → ${j.log}`; }
    else { m.className='msg err'; m.textContent=j.error||'Failed to start.'; }
  }catch(e){ m.className='msg err'; m.textContent=String(e); }
  btn.disabled=false;
}

$('go').onclick = async () => {
  const symbols = chosen(), md = mode();
  if(!symbols.length){ $('msg').className='msg err'; $('msg').textContent='Select at least one symbol.'; return; }
  if(md==='fresh' && !(await confirmDelete(symbols, `Re-run from scratch: delete data for ${symbols.join(', ')}?`))) return;
  await post('/start', {symbols, start:$('start').value, end:$('end').value, mode:md}, $('go'));
};

$('clear').onclick = async () => {
  const symbols = chosen();
  if(!symbols.length){ $('msg').className='msg err'; $('msg').textContent='Select at least one symbol.'; return; }
  if(!(await confirmDelete(symbols, `Clear existing data for ${symbols.join(', ')}?`))) return;
  await post('/clear', {symbols}, $('clear'));
};

function renderStatus(s){
  const running = (s._running_pids||[]).length>0;
  const state = running ? 'running' : (s.state||'idle');
  const lost = Number(s.hours_lost||0);
  const b=$('state');
  if(!running && state==='completed' && lost>0){
    b.textContent=`completed · ${lost} hours lost`; b.className='badge warn';
    if(!$('msg').textContent){ $('msg').className='msg err'; $('msg').textContent=`${lost} hour(s) lost after retries. Run "Repair only" for these symbols.`; }
  } else { b.textContent=state; b.className='badge '+state; }
  $('sub').textContent = (s.date_range||'') + (s.mode?` · ${s.mode}`:'');

  const done=s.days_completed, tot=s.days_total;
  if(tot){
    const pct=Math.min(100,100*done/tot);
    $('prog').style.width=pct+'%';
    $('progtxt').textContent=`${s.current_symbol||''} ${s.symbol_progress||''} · `+
      `${fmt(done)}/${fmt(tot)} days (${pct.toFixed(1)}%) · ${s.symbol_status||''}`;
  } else { $('prog').style.width='0'; $('progtxt').textContent='No active download.'; }

  const updatedAgo = s.updated ? Math.round((Date.now()-new Date(s.updated))/1000)+'s ago' : '—';
  const st=[
    ['Ticks', fmt(s.ticks_total)], ['Rate', s.rate_days_per_sec!=null?s.rate_days_per_sec+' d/s':'—'],
    ['ETA', eta(s.eta_minutes)], ['Retries', fmt(s.http_retries)],
    ['Failures', fmt(s.http_failures)], ['Hours lost', fmt(s.hours_lost)], ['Hours empty', fmt(s.hours_empty)],
    ['Downloaded', s.bytes_downloaded_mb!=null?s.bytes_downloaded_mb+' MB':'—'],
    ['RSS peak', s.rss_peak_mb!=null?s.rss_peak_mb+' MB':'—'], ['Day p95', s.day_p95_seconds!=null?s.day_p95_seconds+'s':'—'],
    ['Updated', updatedAgo],
  ];
  $('stats').innerHTML = st.map(([k,v])=>`<div class="stat"><b>${k}</b>${v}</div>`).join('');
  $('go').disabled = running; $('clear').disabled = running;
}

async function pollStatus(){ try{ renderStatus(await (await fetch('/status')).json()); }catch(e){} }
async function pollLog(){
  try{ const j=await (await fetch('/log?n=200')).json();
    $('logname').textContent=j.file?('· '+j.file):'';
    const el=$('log'); const atBottom=el.scrollTop+el.clientHeight>=el.scrollHeight-30;
    el.textContent=(j.lines||[]).join('\n'); if(atBottom) el.scrollTop=el.scrollHeight;
  }catch(e){}
}

loadConfig();
pollStatus(); pollLog();
setInterval(pollStatus,2000);
setInterval(pollLog,4000);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Local control panel for download.py")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    args = ap.parse_args()

    url = f"http://127.0.0.1:{args.port}"
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"duka-data dashboard → {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.shutdown()


if __name__ == "__main__":
    main()
