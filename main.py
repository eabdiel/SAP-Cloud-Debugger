"""
+--------------------------------------------------------------------------------------+
| SAP RE Web Debugger                                                      |
|--------------------------------------------------------------------------------------|
| Author : Edwin Rodriguez                                                             |
| Date   : 2026-01-14                                                                  |
|--------------------------------------------------------------------------------------|
| Summary                                                                              |
|   Python-based visual debugger for web apps using Chrome DevTools Protocol (CDP).     |
|   - Relaunches Chrome with remote debugging enabled                                  |
|   - Opens SAP Cloud ALM Launchpad                                                    |
|   - Hosts a local UI to stream Network/Console/Debugger events                        |
|   - Provides Trace/Highlight, Threads by requestId, and Response Body viewer          |
|--------------------------------------------------------------------------------------|
| Notes                                                                                |
|   - CDP requires Chrome launched with --remote-debugging-port                         |
|   - This script closes ALL Chrome instances and starts a dedicated debug profile      |
+--------------------------------------------------------------------------------------+
"""

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple

import aiohttp
from aiohttp import web


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------
CDP_PORT = 9222
CDP_HTTP = f"http://127.0.0.1:{CDP_PORT}"

UI_HOST = "127.0.0.1"
UI_PORT = 8765
UI_URL = f"http://{UI_HOST}:{UI_PORT}"

START_URL = "https://google.com" #replace with own cloud instance url
PROFILE_DIR = Path(os.environ.get("TEMP", "/tmp")) / "chrome-cdp-profile"


# --------------------------------------------------------------------------------------
# Utility: close Chrome (forcefully)
# --------------------------------------------------------------------------------------
def kill_chrome() -> None:
    """Force-kill Chrome so we can relaunch it in CDP debug mode (kills ALL Chrome windows)."""
    system = platform.system().lower()
    if "windows" in system:
        subprocess.run(["taskkill", "/F", "/IM", "chrome.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif "darwin" in system:
        subprocess.run(["pkill", "-f", "Google Chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["pkill", "-f", "chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def find_chrome_exe() -> str:
    """Locate Chrome executable based on OS."""
    system = platform.system().lower()

    if "windows" in system:
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        for c in candidates:
            if Path(c).exists():
                return c
        exe = shutil.which("chrome") or shutil.which("chrome.exe")
        if exe:
            return exe
        raise FileNotFoundError("Chrome not found. Install Chrome or adjust find_chrome_exe().")

    if "darwin" in system:
        c = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if Path(c).exists():
            return c
        exe = shutil.which("google-chrome") or shutil.which("chrome")
        if exe:
            return exe
        raise FileNotFoundError("Google Chrome not found. Install Chrome or adjust find_chrome_exe().")

    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        exe = shutil.which(name)
        if exe:
            return exe
    raise FileNotFoundError("Chrome/Chromium not found. Install it or add it to PATH.")


def launch_chrome() -> None:
    """Launch Chrome with CDP enabled and open Cloud ALM."""
    chrome = find_chrome_exe()
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    args = [
        chrome,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={str(PROFILE_DIR)}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        START_URL,
    ]

    if platform.system().lower().startswith("windows"):
        subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
    else:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


async def wait_for_cdp(timeout_sec: int = 20) -> bool:
    """Wait for Chrome CDP endpoint to become available."""
    deadline = time.time() + timeout_sec
    async with aiohttp.ClientSession() as session:
        while time.time() < deadline:
            try:
                async with session.get(f"{CDP_HTTP}/json/version", timeout=2) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
    return False


# --------------------------------------------------------------------------------------
# Minimal CDP Client
# --------------------------------------------------------------------------------------
class CDPClient:
    """Small CDP websocket client to send commands and receive events."""

    def __init__(self):
        self.ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._handlers = []
        self._session: Optional[aiohttp.ClientSession] = None

    async def list_tabs(self) -> List[Dict[str, Any]]:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{CDP_HTTP}/json") as r:
                return await r.json()

    async def connect(self, ws_url: str) -> None:
        self._session = aiohttp.ClientSession()
        self.ws = await self._session.ws_connect(ws_url, heartbeat=30)
        asyncio.create_task(self._reader())

    def on_event(self, fn) -> None:
        self._handlers.append(fn)

    async def _reader(self) -> None:
        assert self.ws
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)

                if "id" in data and data["id"] in self._pending:
                    fut = self._pending.pop(data["id"])
                    if not fut.done():
                        fut.set_result(data)
                    continue

                if "method" in data:
                    for fn in self._handlers:
                        try:
                            await fn(data)
                        except Exception:
                            pass

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.ws:
            raise RuntimeError("CDP websocket not connected")

        self._id += 1
        msg_id = self._id
        payload: Dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params

        fut = asyncio.get_event_loop().create_future()
        self._pending[msg_id] = fut
        await self.ws.send_str(json.dumps(payload))
        return await fut


# --------------------------------------------------------------------------------------
# UI HTML
#   Layout changes:
#     - Use CSS Grid for overall structure (no overlap)
#     - Introduce a draggable split bar between Threads and Feed
#     - Ensure <pre> has horizontal scroll (white-space: pre; overflow-x: auto)
# --------------------------------------------------------------------------------------
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Arthrex-SAP COE-RE Web Debugger</title>
  <style>
    :root{
      --gap: 12px;
      --panel-radius: 12px;
      --threadsW: 520px; /* default width; user can drag-resize */
    }

    * { box-sizing: border-box; }
    body { font-family: Arial, sans-serif; margin: 16px; }

    .mono { font-family: ui-monospace, Menlo, Consolas, monospace; }
    .small { font-size: 12px; color: #444; }
    .muted { color:#666; font-size:12px; }

    input, button, select, textarea { width: 100%; padding: 8px; margin: 6px 0; }
    textarea { height: 140px; font-family: ui-monospace, Menlo, Consolas, monospace; }

    .panel { border: 1px solid #ddd; background:#fff; border-radius: var(--panel-radius); padding: 10px; }
    .panel-title { font-weight: 700; margin-bottom: 6px; }

    /* Main page grid: left controls, right viewer */
    .page {
      display: grid;
      grid-template-columns: minmax(360px, 420px) 1fr;
      gap: var(--gap);
      align-items: start;
    }

    /* Right side: threads | divider | feed */
    .viewer {
      display: grid;
      grid-template-columns: var(--threadsW) 8px 1fr;
      gap: var(--gap);
      align-items: start;
      min-width: 0;
    }

    /* Divider for resizing columns */
    .divider {
      width: 8px;
      cursor: col-resize;
      border-radius: 999px;
      background: linear-gradient(#eee, #ddd);
      height: 100%;
      min-height: 720px; /* helps grab area */
    }
    .divider:hover { background: linear-gradient(#ddd, #ccc); }

    /* Make panels internal-scroll instead of overlapping */
    .tlist, .log {
      overflow: auto;
      border: 1px solid #ddd;
      background: #fafafa;
      border-radius: var(--panel-radius);
      padding: 10px;
      height: 560px;
      min-width: 0;
    }

    /* Threads list items */
    .titem { border:1px solid #e4e4e4; background:#fff; border-radius:10px; padding:8px; margin:6px 0; cursor:pointer; }
    .titem:hover { border-color:#c9c9c9; }
    .titem.active { border-color:#ff3b3b; box-shadow: 0 0 0 2px rgba(255,59,59,0.15); }
    .trow { display:flex; gap:10px; align-items:center; }
    .tmeta { font-size: 12px; color:#444; }
    .turl { font-size: 11px; color:#666; margin-top:4px; word-break: break-all; }
    .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid transparent; background:#eee; }

    /* Event cards */
    .evt { border: 1px solid #e4e4e4; background: #fff; border-radius: 10px; padding: 8px 10px; margin: 8px 0; }
    .evt summary { cursor: pointer; list-style: none; }
    .evt summary::-webkit-details-marker { display: none; }
    .evt-head { display:flex; gap:10px; align-items:center; }
    .evt-time { font-size: 11px; color:#666; width: 96px; flex: 0 0 auto; }
    .evt-method { font-weight: 700; font-size: 12px; }
    .badge { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid transparent; }

    /* Color categories */
    .b-network { background:#eef6ff; border-color:#cfe6ff; color:#124c8a; }
    .b-console { background:#f6f0ff; border-color:#e3d6ff; color:#4a257a; }
    .b-debug   { background:#fff4e6; border-color:#ffe0b2; color:#7a3d00; }
    .b-error   { background:#ffecec; border-color:#ffc9c9; color:#7a0b0b; }

    /* JSON viewer: horizontal scroll enabled */
    pre {
      margin: 8px 0 0 0;
      padding: 10px;
      background:#0b1020;
      color:#e7e7e7;
      border-radius: 10px;
      overflow-x: auto;
      overflow-y: auto;
      white-space: pre;   /* critical: do not wrap, allow horizontal scroll */
      max-width: 100%;
    }

    .k { color:#9cdcfe; }
    .s { color:#ce9178; }
    .n { color:#b5cea8; }
    .b { color:#569cd6; }
    .null { color:#808080; }

    .copyhint { font-size: 11px; color:#888; margin-top:6px; }
    .hit { background: #ff3b3b; color: #fff; padding: 1px 3px; border-radius: 4px; }
    .chip { display:inline-flex; gap:6px; align-items:center; padding: 4px 8px; border:1px solid #ddd; border-radius: 999px; background:#fff; font-size:12px; }
    .chip strong { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }

    /* Trace toolbar */
    .toolbar { display:flex; gap:10px; align-items:center; }
    .toolbar > * { flex: 1; }
    .toolbar .btn { flex: 0.5; }

    /* Response body viewer: fixed size, never overlaps */
    .bodywrap { margin-top: 10px; }
    .bodyhdr { display:flex; gap:10px; align-items:center; }
    .bodyhdr button { width:auto; padding: 6px 10px; margin: 0; }
    .bodybox {
      margin-top: 8px;
      height: 240px;
      overflow: auto;
      border:1px solid #ddd;
      border-radius: var(--panel-radius);
      padding: 10px;
      background:#0b1020;
      color:#e7e7e7;
      white-space: pre;   /* allow horizontal scrolling */
    }

    /* Responsive: stack columns on narrow screens */
    @media (max-width: 1200px) {
      .page { grid-template-columns: 1fr; }
      .viewer { grid-template-columns: 1fr; }
      .divider { display: none; }
      :root { --threadsW: 100%; }
      .tlist, .log { height: 420px; }
    }
  </style>
</head>
<body>
  <h2>Arthrex-SAP COE-RE Web Debugger</h2>
  <div class="small mono">CDP: 127.0.0.1:9222 • UI: 127.0.0.1:8765</div>

  <div class="page">
    <!-- LEFT CONTROL COLUMN -->
    <div>
      <div class="panel">
        <div class="panel-title">Select Tab</div>
        <button onclick="refreshTabs()">Refresh Tabs</button>
        <select id="tabs"></select>
        <button onclick="connectTab()">Connect</button>
        <div class="small" id="status">Not connected</div>
      </div>

      <div class="panel" style="margin-top:12px;">
        <div class="panel-title">Trace / Highlight</div>
        <div class="toolbar">
          <input id="trace" placeholder="Type a key (requestId) or a value to highlight everywhere" />
          <button class="btn" onclick="setTrace()">Apply</button>
          <button class="btn" onclick="clearTrace()">Clear</button>
        </div>
        <div class="small">
          Tip: click any value inside JSON to copy it, then paste it here to highlight where it appears and the value it had/has.
        </div>
        <div id="traceState" class="small"></div>
      </div>

      <div class="panel" style="margin-top:12px;">
        <div class="panel-title">Evaluate (Get/Set)</div>
        <input id="expr" placeholder="window.location.href OR myVar = 123" />
        <button onclick="evalExpr()">Evaluate</button>
        <textarea id="evalOut" readonly></textarea>
      </div>

      <div class="panel" style="margin-top:12px;">
        <div class="panel-title">Debug Controls</div>
        <div class="toolbar">
          <button onclick="dbg('pause')">Pause</button>
          <button onclick="dbg('resume')">Resume</button>
        </div>
        <div class="toolbar">
          <button onclick="dbg('stepOver')">Step Over</button>
          <button onclick="dbg('stepInto')">Step Into</button>
          <button onclick="dbg('stepOut')">Step Out</button>
        </div>
      </div>
    </div>

    <!-- RIGHT VIEWER COLUMN -->
    <div class="viewer">
      <!-- Threads -->
      <div class="panel">
        <div class="panel-title">Threads (grouped by requestId)</div>
        <div class="small muted">Click a thread to filter the feed. Drag the divider to resize columns.</div>
        <button onclick="clearThreadFilter()">Clear Thread Filter</button>
        <div class="tlist" id="threads"></div>

        <div class="bodywrap">
          <div class="bodyhdr">
            <div class="panel-title" style="margin:0;">Response Body</div>
            <button onclick="fetchBodyForSelected()">Fetch Body</button>
          </div>
          <div class="small mono" id="bodyMeta"></div>
          <div class="bodybox mono" id="bodyBox">Select a thread and click "Fetch Body".</div>
        </div>
      </div>

      <!-- Divider (resizable split) -->
      <div class="divider" id="divider" title="Drag to resize"></div>

      <!-- Feed -->
      <div class="panel">
        <div class="panel-title">Live Feed</div>
        <div class="small muted">
          Legend:
          <span class="badge b-network">Network</span>
          <span class="badge b-console">Console</span>
          <span class="badge b-debug">Debugger</span>
          <span class="badge b-error">Error</span>
        </div>
        <div class="log" id="log"></div>
        <div class="copyhint">Click any value in the JSON to copy.</div>
      </div>
    </div>
  </div>

<script>
  // -----------------------------------------------------------------------------------
  // Front-end notes:
  //   - EVENTS[] stores all incoming events so we can re-render on filter/trace changes.
  //   - THREADS map is derived from Network events grouped by requestId.
  //   - Divider supports drag-to-resize by updating CSS variable --threadsW.
  // -----------------------------------------------------------------------------------

  let ws;
  const TARGET_HINT = "alm.cloud.sap/launchpad";

  // Trace state
  let TRACE = "";
  let traceHits = 0;
  const lastSeen = new Map();

  // Event store + filtering
  const EVENTS = [];
  let FILTER_REQUEST_ID = "";
  let SELECTED_REQUEST_ID = "";

  // Thread store
  const THREADS = new Map();

  // ----------- Resizable split (threads vs feed) -----------
  (function initDivider(){
    const divider = document.getElementById("divider");
    if (!divider) return;

    let dragging = false;

    const onDown = (e) => {
      dragging = true;
      document.body.style.userSelect = "none";
      divider.setPointerCapture?.(e.pointerId);
    };

    const onMove = (e) => {
      if (!dragging) return;
      // Compute desired width from viewport left edge to cursor, minus left column width + gap
      // We can simpler: use the viewer container's left position
      const viewer = document.querySelector(".viewer");
      if (!viewer) return;

      const rect = viewer.getBoundingClientRect();
      // threads width = mouseX - viewerLeft
      let w = e.clientX - rect.left;
      // clamp reasonable limits
      w = Math.max(300, Math.min(w, 900));
      document.documentElement.style.setProperty("--threadsW", w + "px");
    };

    const onUp = () => {
      dragging = false;
      document.body.style.userSelect = "auto";
    };

    divider.addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  })();

  // ----------- Utility helpers -----------
  function flashStatus(text) {
    const el = document.getElementById("status");
    const old = el.textContent;
    el.textContent = text;
    setTimeout(() => el.textContent = old, 1200);
  }

  async function copyText(txt) {
    try {
      await navigator.clipboard.writeText(txt);
      flashStatus(`Copied: ${txt}`);
    } catch {
      flashStatus("Copy failed (clipboard blocked).");
    }
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function categoryFromMethod(method) {
    if (!method) return {cls:"b-error", label:"Unknown"};
    if (method.startsWith("Network.")) return {cls:"b-network", label:"Network"};
    if (method.startsWith("Runtime.") || method.startsWith("Log.")) return {cls:"b-console", label:"Console"};
    if (method.startsWith("Debugger.")) return {cls:"b-debug", label:"Debugger"};
    return {cls:"b-error", label:"Other"};
  }

  // ----------- Trace highlighting -----------
  function makeHit(text) {
    if (!TRACE) return escapeHtml(text);
    const idx = text.indexOf(TRACE);
    if (idx === -1) return escapeHtml(text);
    traceHits++;
    return escapeHtml(text.slice(0, idx)) +
           `<span class="hit">${escapeHtml(TRACE)}</span>` +
           escapeHtml(text.slice(idx + TRACE.length));
  }

  function jsonSyntax(obj) {
    const t = typeof obj;

    if (obj === null) return `<span class="null">null</span>`;

    if (t === "string") {
      const raw = obj;
      const shown = makeHit(raw);
      return `<span class="s" data-copy="${escapeHtml(raw)}">"${shown}"</span>`;
    }
    if (t === "number") {
      const raw = String(obj);
      const shown = makeHit(raw);
      return `<span class="n" data-copy="${escapeHtml(raw)}">${shown}</span>`;
    }
    if (t === "boolean") {
      const raw = String(obj);
      const shown = makeHit(raw);
      return `<span class="b" data-copy="${escapeHtml(raw)}">${shown}</span>`;
    }

    if (Array.isArray(obj)) {
      const items = obj.map(v => jsonSyntax(v)).join(", ");
      return `[${items}]`;
    }

    const keys = Object.keys(obj);
    const parts = [];
    for (const k of keys) {
      const keyStr = String(k);
      const keyHit = (TRACE && keyStr.includes(TRACE));
      if (keyHit) traceHits++;

      if (TRACE && keyStr === TRACE) {
        const v = obj[k];
        const vStr = (typeof v === "string" || typeof v === "number" || typeof v === "boolean") ? String(v) : "[object]";
        lastSeen.set(TRACE, vStr);
      }

      const keyHtml = keyHit ? `<span class="k hit">${escapeHtml(keyStr)}</span>` : `<span class="k">${escapeHtml(keyStr)}</span>`;
      parts.push(`${keyHtml}: ${jsonSyntax(obj[k])}`);
    }
    return `{ ${parts.join(", ")} }`;
  }

  function attachCopyHandlers(container) {
    container.querySelectorAll("[data-copy]").forEach(el => {
      el.style.cursor = "copy";
      el.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        const v = el.getAttribute("data-copy");
        copyText(v);
      });
    });
  }

  // ----------- Threads (requestId grouping) -----------
  function extractRequestId(params) {
    if (!params) return "";
    return params.requestId || "";
  }

  function upsertThreadFromEvent(evt) {
    const { method, params, _ts } = evt;
    if (!method || !params || !method.startsWith("Network.")) return;

    const rid = extractRequestId(params);
    if (!rid) return;

    const current = THREADS.get(rid) || {
      requestId: rid,
      url: "",
      method: "",
      status: "",
      mimeType: "",
      lastEventTs: _ts,
      count: 0
    };

    current.count += 1;
    current.lastEventTs = _ts;

    if (method === "Network.requestWillBeSent" && params.request) {
      current.url = params.request.url || current.url;
      current.method = params.request.method || current.method;
    }

    if (method === "Network.responseReceived" && params.response) {
      current.status = String(params.response.status ?? current.status);
      current.mimeType = String(params.response.mimeType ?? current.mimeType);
      current.url = params.response.url || current.url;
    }

    if (method === "Network.loadingFailed") {
      current.status = "FAILED";
    }

    THREADS.set(rid, current);
  }

  function renderThreads() {
    const box = document.getElementById("threads");
    box.innerHTML = "";

    const arr = Array.from(THREADS.values()).sort((a, b) => (b.lastEventTs - a.lastEventTs));

    for (const t of arr) {
      const active = (t.requestId === SELECTED_REQUEST_ID);
      const statusPill = t.status
        ? `<span class="pill ${t.status === "FAILED" ? "b-error" : "b-network"}">${escapeHtml(t.status)}</span>`
        : `<span class="pill b-network">...</span>`;

      const item = document.createElement("div");
      item.className = "titem" + (active ? " active" : "");
      item.innerHTML = `
        <div class="trow">
          ${statusPill}
          <div class="tmeta mono"><strong>${escapeHtml(t.method || "")}</strong></div>
          <div style="margin-left:auto;" class="tmeta mono">events: ${t.count}</div>
        </div>
        <div class="turl mono">${escapeHtml(t.url || t.requestId)}</div>
        <div class="tmeta mono">requestId: ${escapeHtml(t.requestId)}</div>
      `;
      item.addEventListener("click", () => selectThread(t.requestId));
      box.appendChild(item);
    }
  }

  function selectThread(requestId) {
    SELECTED_REQUEST_ID = requestId;
    FILTER_REQUEST_ID = requestId;
    document.getElementById("bodyMeta").textContent = `Selected requestId: ${requestId}`;
    document.getElementById("bodyBox").textContent = `Selected requestId: ${requestId}\nClick "Fetch Body" to attempt to load response body.`;
    renderAll();
    renderThreads();
  }

  function clearThreadFilter() {
    FILTER_REQUEST_ID = "";
    SELECTED_REQUEST_ID = "";
    document.getElementById("bodyMeta").textContent = "";
    document.getElementById("bodyBox").textContent = 'Select a thread and click "Fetch Body".';
    renderAll();
    renderThreads();
  }

  // ----------- Event rendering + filtering -----------
  function renderEventCard(evt) {
    traceHits = 0;

    const method = evt.method || "";
    const params = evt.params || {};
    const cat = categoryFromMethod(method);

    let headline = "";
    if (method === "Network.requestWillBeSent" && params.request && params.request.url) {
      headline = params.request.method ? `${params.request.method} ${params.request.url}` : params.request.url;
    } else if (method === "Network.responseReceived" && params.response && params.response.url) {
      headline = `${params.response.status} ${params.response.url}`;
    } else if (method === "Runtime.consoleAPICalled" && params.args && params.args.length) {
      headline = params.args.map(a => a.value ?? a.description ?? "").join(" ");
    } else if (method === "Runtime.exceptionThrown" && params.exceptionDetails) {
      headline = params.exceptionDetails.text || "Exception";
    } else if (method === "Debugger.paused" && params.reason) {
      headline = `Paused: ${params.reason}`;
    }

    const time = new Date(evt._ts).toLocaleTimeString([], {hour12:false});
    const jsonHtml = jsonSyntax(params);
    const hitChip = TRACE ? `<span class="chip">hits: <strong>${traceHits}</strong></span>` : "";
    const headlineHtml = headline ? `<div class="small mono">${escapeHtml(headline)}</div>` : "";

    return `
      <details class="evt" open>
        <summary>
          <div class="evt-head">
            <div class="evt-time mono">${time}</div>
            <span class="badge ${cat.cls}">${cat.label}</span>
            <div class="evt-method mono">${escapeHtml(method)}</div>
            <div style="margin-left:auto">${hitChip}</div>
          </div>
          ${headlineHtml}
        </summary>
        <pre class="mono">${jsonHtml}</pre>
      </details>
    `;
  }

  function eventPassesFilter(evt) {
    if (!FILTER_REQUEST_ID) return true;
    const rid = extractRequestId(evt.params);
    return rid === FILTER_REQUEST_ID;
  }

  function renderAll() {
    const log = document.getElementById("log");
    log.innerHTML = "";

    for (const evt of EVENTS) {
      if (!eventPassesFilter(evt)) continue;
      const wrapper = document.createElement("div");
      wrapper.innerHTML = renderEventCard(evt);
      log.appendChild(wrapper);
      attachCopyHandlers(wrapper);
    }

    updateTraceState();
    log.scrollTop = log.scrollHeight;
  }

  // ----------- Trace controls -----------
  function updateTraceState() {
    const el = document.getElementById("traceState");
    if (!TRACE) { el.textContent = ""; return; }
    const last = lastSeen.get(TRACE);
    if (last !== undefined) {
      el.innerHTML = `Tracing <span class="chip"><strong>${escapeHtml(TRACE)}</strong></span> • last seen: <span class="chip"><strong>${escapeHtml(last)}</strong></span>`;
    } else {
      el.innerHTML = `Tracing <span class="chip"><strong>${escapeHtml(TRACE)}</strong></span>`;
    }
  }

  function setTrace() {
    TRACE = (document.getElementById("trace").value || "").trim();
    lastSeen.clear();
    renderAll();
    flashStatus(TRACE ? `Trace set: ${TRACE}` : "Trace cleared");
  }

  function clearTrace() {
    document.getElementById("trace").value = "";
    setTrace();
  }

  // ----------- Response body fetch -----------
  function fetchBodyForSelected() {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      flashStatus("Not connected to server.");
      return;
    }
    if (!SELECTED_REQUEST_ID) {
      flashStatus("Select a thread first.");
      return;
    }
    document.getElementById("bodyMeta").textContent = `Selected requestId: ${SELECTED_REQUEST_ID} • fetching...`;
    ws.send(JSON.stringify({ type: "getBody", requestId: SELECTED_REQUEST_ID }));
  }

  function showBodyResult(payload) {
    const meta = document.getElementById("bodyMeta");
    const box = document.getElementById("bodyBox");

    if (!payload.ok) {
      meta.textContent = `Selected requestId: ${payload.requestId} • error`;
      box.textContent = payload.error || "Body not available.";
      return;
    }

    meta.textContent = `Selected requestId: ${payload.requestId} • mimeType: ${payload.mimeType || "?"} • base64: ${payload.base64Encoded ? "true" : "false"}`;

    const text = payload.body || "";
    if (payload.base64Encoded) {
      box.textContent = text;
      return;
    }

    try {
      const obj = JSON.parse(text);
      box.textContent = JSON.stringify(obj, null, 2);
    } catch {
      box.textContent = text;
    }
  }

  // ----------- Tabs + connection -----------
  async function refreshTabs() {
    const r = await fetch('/api/tabs');
    const tabs = await r.json();
    const sel = document.getElementById('tabs');
    sel.innerHTML = '';

    let bestIndex = 0;
    tabs.forEach((t, i) => {
      const opt = document.createElement('option');
      opt.value = t.id;
      opt.textContent = `${t.title} — ${t.url}`;
      sel.appendChild(opt);
      if ((t.url || "").includes(TARGET_HINT)) bestIndex = i;
    });
    sel.selectedIndex = bestIndex;
  }

  function connectTab() {
    const tabId = document.getElementById('tabs').value;

    // reset state
    EVENTS.length = 0;
    THREADS.clear();
    FILTER_REQUEST_ID = "";
    SELECTED_REQUEST_ID = "";
    document.getElementById("bodyMeta").textContent = "";
    document.getElementById("bodyBox").textContent = 'Select a thread and click "Fetch Body".';
    document.getElementById("threads").innerHTML = "";
    document.getElementById("log").innerHTML = "";

    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => {
      ws.send(JSON.stringify({type:'connect', tabId}));
      document.getElementById('status').textContent = 'Connecting...';
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);

      if (msg.type === 'status') {
        document.getElementById('status').textContent = msg.message;
        return;
      }
      if (msg.type === 'evalResult') {
        document.getElementById('evalOut').value = JSON.stringify(msg.result, null, 2);
        return;
      }
      if (msg.type === 'bodyResult') {
        showBodyResult(msg);
        return;
      }

      if (msg.type === 'event') {
        msg._ts = Date.now();
        EVENTS.push(msg);
        upsertThreadFromEvent(msg);

        renderThreads();
        renderAll();
        return;
      }
    };

    ws.onclose = () => document.getElementById('status').textContent = 'Disconnected';
  }

  // ----------- Evaluate + Debug controls -----------
  function evalExpr() {
    const expr = document.getElementById('expr').value;
    ws.send(JSON.stringify({type:'eval', expression: expr}));
  }

  function dbg(action) {
    ws.send(JSON.stringify({type:'debug', action}));
  }

  // initial load
  refreshTabs();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------------------
# Server-side state
# --------------------------------------------------------------------------------------
class AppState:
    """Global state for CDP + UI websocket clients + request metadata."""
    def __init__(self):
        self.cdp = CDPClient()
        self.connected = False
        self.ws_clients: List[web.WebSocketResponse] = []
        self.request_meta: Dict[str, Tuple[str, str]] = {}  # requestId -> (mimeType, url)


state = AppState()


async def broadcast(msg: Dict[str, Any]) -> None:
    """Broadcast a JSON message to all UI clients."""
    dead = []
    for ws in state.ws_clients:
        try:
            await ws.send_str(json.dumps(msg))
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in state.ws_clients:
            state.ws_clients.remove(ws)


async def cdp_event_handler(evt: Dict[str, Any]) -> None:
    """Receive CDP events and forward the useful subset to the UI."""
    method = evt.get("method", "")
    params = evt.get("params", {}) or {}

    if method == "Network.responseReceived":
        rid = params.get("requestId")
        resp = params.get("response", {}) or {}
        if rid:
            mime = str(resp.get("mimeType", ""))
            url = str(resp.get("url", ""))
            state.request_meta[rid] = (mime, url)

    if method in (
        "Network.requestWillBeSent",
        "Network.responseReceived",
        "Network.loadingFailed",
        "Runtime.consoleAPICalled",
        "Runtime.exceptionThrown",
        "Debugger.paused",
        "Debugger.resumed",
    ):
        await broadcast({"type": "event", "method": method, "params": params})


# --------------------------------------------------------------------------------------
# HTTP handlers
# --------------------------------------------------------------------------------------
async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def api_tabs(request: web.Request) -> web.Response:
    tabs = await state.cdp.list_tabs()
    out = []
    for t in tabs:
        if t.get("type") == "page":
            out.append(
                {
                    "id": t.get("id"),
                    "title": t.get("title", "(no title)"),
                    "url": t.get("url", ""),
                    "webSocketDebuggerUrl": t.get("webSocketDebuggerUrl"),
                }
            )
    return web.json_response(out)


# --------------------------------------------------------------------------------------
# WebSocket handler (UI <-> server)
# --------------------------------------------------------------------------------------
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    state.ws_clients.append(ws)

    await ws.send_str(json.dumps({"type": "status", "message": "UI connected. Select a tab and click Connect."}))

    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue

        data = json.loads(msg.data)
        mtype = data.get("type")

        if mtype == "connect":
            tab_id = data.get("tabId")
            tabs = await state.cdp.list_tabs()
            target = next((x for x in tabs if x.get("id") == tab_id), None)

            if not target or not target.get("webSocketDebuggerUrl"):
                await ws.send_str(json.dumps({"type": "status", "message": "Tab not found or missing WS URL."}))
                continue

            state.request_meta = {}

            await state.cdp.connect(target["webSocketDebuggerUrl"])
            state.cdp.on_event(cdp_event_handler)

            await state.cdp.send("Runtime.enable")
            await state.cdp.send("Network.enable")
            await state.cdp.send("Log.enable")
            await state.cdp.send("Debugger.enable")
            await state.cdp.send("Runtime.setAsyncCallStackDepth", {"maxDepth": 32})

            state.connected = True
            await ws.send_str(json.dumps({"type": "status", "message": "Connected. Streaming events."}))

        elif mtype == "eval":
            if not state.connected:
                await ws.send_str(json.dumps({"type": "evalResult", "result": {"error": "Not connected"}}))
                continue

            expr = data.get("expression", "")
            resp = await state.cdp.send(
                "Runtime.evaluate",
                {
                    "expression": expr,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            await ws.send_str(json.dumps({"type": "evalResult", "result": resp}))

        elif mtype == "debug":
            if not state.connected:
                await broadcast({"type": "event", "method": "error", "params": {"message": "Not connected"}})
                continue

            action = data.get("action")
            method_map = {
                "pause": "Debugger.pause",
                "resume": "Debugger.resume",
                "stepOver": "Debugger.stepOver",
                "stepInto": "Debugger.stepInto",
                "stepOut": "Debugger.stepOut",
            }
            cdp_method = method_map.get(action)
            if not cdp_method:
                await broadcast({"type": "event", "method": "error", "params": {"message": f"Unknown action {action}"}})
                continue
            await state.cdp.send(cdp_method)

        elif mtype == "getBody":
            if not state.connected:
                await ws.send_str(json.dumps({"type": "bodyResult", "ok": False, "error": "Not connected", "requestId": ""}))
                continue

            request_id = str(data.get("requestId", "")).strip()
            if not request_id:
                await ws.send_str(json.dumps({"type": "bodyResult", "ok": False, "error": "Missing requestId", "requestId": ""}))
                continue

            mime, url = state.request_meta.get(request_id, ("", ""))

            try:
                resp = await state.cdp.send("Network.getResponseBody", {"requestId": request_id})
                result = resp.get("result", {}) or {}
                body = result.get("body", "")
                base64_encoded = bool(result.get("base64Encoded", False))

                await ws.send_str(
                    json.dumps(
                        {
                            "type": "bodyResult",
                            "ok": True,
                            "requestId": request_id,
                            "mimeType": mime,
                            "url": url,
                            "base64Encoded": base64_encoded,
                            "body": body,
                        }
                    )
                )
            except Exception as e:
                await ws.send_str(
                    json.dumps(
                        {
                            "type": "bodyResult",
                            "ok": False,
                            "requestId": request_id,
                            "mimeType": mime,
                            "url": url,
                            "error": f"Body not available: {e}",
                        }
                    )
                )

    if ws in state.ws_clients:
        state.ws_clients.remove(ws)
    return ws


# --------------------------------------------------------------------------------------
# Server start
# --------------------------------------------------------------------------------------
async def run_ui_server() -> None:
    """Start local UI server and auto-open in default browser."""
    app = web.Application()
    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/tabs", api_tabs),
            web.get("/ws", ws_handler),
        ]
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, UI_HOST, UI_PORT)
    await site.start()

    print(f"UI running at {UI_URL}")
    webbrowser.open(UI_URL)

    while True:
        await asyncio.sleep(3600)


async def main() -> None:
    """Orchestrate: close Chrome -> relaunch in CDP -> wait -> start UI."""
    print("Closing Chrome...")
    kill_chrome()
    await asyncio.sleep(1.0)

    print("Launching Chrome with remote debugging + Cloud ALM...")
    launch_chrome()

    print("Waiting for CDP...")
    ok = await wait_for_cdp(timeout_sec=20)
    if not ok:
        print(f"ERROR: CDP not reachable at {CDP_HTTP}.")
        sys.exit(1)

    print("Starting UI (and auto-opening it)...")
    await run_ui_server()


if __name__ == "__main__":
    asyncio.run(main())
