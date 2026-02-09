"""
+--------------------------------------------------------------------------------------+
| Arthrex-SAP COE-RE Web Debugger                                                      |
|--------------------------------------------------------------------------------------|
| Author : Edwin Rodriguez                                                             |
| Date   : 2026-01-14                                                                  |
|--------------------------------------------------------------------------------------|
| Summary                                                                              |
|   A lightweight, Python-based visual debugger for web apps using Chrome DevTools      |
|   Protocol (CDP). This tool:                                                         |
|     - Force-relaunches Chrome with remote debugging enabled                           |
|     - Opens SAP Cloud ALM Launchpad                                                   |
|     - Hosts a local UI (http://127.0.0.1:8765)                                        |
|     - Streams Network/Console/Debugger events into a readable UI                      |
|     - Provides Trace/Highlight + click-to-copy to follow parameters (e.g. requestId) |
|     - Adds a "Threads" view that groups events by requestId                           |
|     - Allows fetching and viewing response bodies via Network.getResponseBody         |
|                                                                                      |
| Notes                                                                                |
|   - CDP requires Chrome to be launched with --remote-debugging-port.                  |
|   - This script will close ALL Chrome instances and reopen a dedicated debug profile. |
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

# SAP Cloud ALM portal URL (Arthrex org)
START_URL = "https://arthrex-cloudalm.eu10-004.alm.cloud.sap/launchpad#Shell-home"

# Isolated Chrome profile used for the debug instance only (does not touch your normal profile)
PROFILE_DIR = Path(os.environ.get("TEMP", "/tmp")) / "chrome-cdp-profile"


# --------------------------------------------------------------------------------------
# Utility: close Chrome (forcefully)
# --------------------------------------------------------------------------------------
def kill_chrome() -> None:
    """
    Force-kill Chrome so we can relaunch it in CDP debug mode.

    WARNING:
      - This will close ALL Chrome windows.
      - Anything unsaved in Chrome will be lost.
    """
    system = platform.system().lower()
    if "windows" in system:
        subprocess.run(
            ["taskkill", "/F", "/IM", "chrome.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    elif "darwin" in system:
        subprocess.run(["pkill", "-f", "Google Chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.run(["pkill", "-f", "chrome"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["pkill", "-f", "chromium"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# --------------------------------------------------------------------------------------
# Utility: locate Chrome executable
# --------------------------------------------------------------------------------------
def find_chrome_exe() -> str:
    """
    Attempt to find Google Chrome (or Chromium) executable based on OS.
    """
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

    # Linux
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        exe = shutil.which(name)
        if exe:
            return exe
    raise FileNotFoundError("Chrome/Chromium not found. Install it or add it to PATH.")


# --------------------------------------------------------------------------------------
# Launch Chrome with remote debugging enabled
# --------------------------------------------------------------------------------------
def launch_chrome() -> None:
    """
    Launch a new Chrome instance with:
      - CDP remote debugging enabled on localhost
      - isolated user-data-dir profile
      - opens Cloud ALM Launchpad URL
    """
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

    # Run detached so Python continues
    if platform.system().lower().startswith("windows"):
        subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS, close_fds=True)
    else:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


async def wait_for_cdp(timeout_sec: int = 20) -> bool:
    """
    Wait for Chrome's CDP HTTP endpoint to become available.
    """
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
    """
    A very small CDP WebSocket client.

    Key concepts:
      - list_tabs() hits the CDP HTTP endpoint to enumerate targets (tabs/pages)
      - connect(ws_url) attaches to a specific target's WebSocket endpoint
      - send(method, params) issues a CDP command and awaits its response
      - on_event(fn) registers handlers for CDP events (Network.*, Runtime.*, Debugger.*, etc.)
    """

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
        """
        Read loop for the CDP WebSocket.
        """
        assert self.ws
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)

                # Response to a command
                if "id" in data and data["id"] in self._pending:
                    fut = self._pending.pop(data["id"])
                    if not fut.done():
                        fut.set_result(data)
                    continue

                # CDP Event
                if "method" in data:
                    for fn in self._handlers:
                        try:
                            await fn(data)
                        except Exception:
                            # event handlers should never crash the CDP read loop
                            pass

    async def send(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Send a CDP command and await response.
        """
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
# UI HTML (Single-page app served by aiohttp)
# --------------------------------------------------------------------------------------
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Arthrex-SAP COE-RE Web Debugger</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 16px; }
    .row { display: flex; gap: 12px; }
    .col { flex: 1; min-width: 420px; }
    input, button, select, textarea { width: 100%; padding: 8px; margin: 6px 0; }
    textarea { height: 140px; font-family: ui-monospace, Menlo, Consolas, monospace; }

    .small { font-size: 12px; color: #444; }
    .mono { font-family: ui-monospace, Menlo, Consolas, monospace; }
    .toolbar { display:flex; gap:10px; align-items:center; }
    .toolbar > * { flex: 1; }
    .toolbar .btn { flex: 0.5; }

    /* Layout panels */
    .panel { border: 1px solid #ddd; background:#fff; border-radius: 12px; padding: 10px; }
    .panel-title { font-weight: 700; margin-bottom: 6px; }
    .split { display:flex; gap:12px; }
    .threads { width: 38%; min-width: 320px; max-width: 520px; }
    .feed { flex: 1; }

    /* Threads list */
    .tlist { height: 520px; overflow:auto; border:1px solid #eee; border-radius:10px; padding:8px; background:#fafafa; }
    .titem { border:1px solid #e4e4e4; background:#fff; border-radius:10px; padding:8px; margin:6px 0; cursor:pointer; }
    .titem:hover { border-color:#c9c9c9; }
    .titem.active { border-color:#ff3b3b; box-shadow: 0 0 0 2px rgba(255,59,59,0.15); }
    .trow { display:flex; gap:10px; align-items:center; }
    .tmeta { font-size: 12px; color:#444; }
    .turl { font-size: 11px; color:#666; margin-top:4px; word-break: break-all; }
    .pill { font-size: 11px; padding: 2px 8px; border-radius: 999px; border: 1px solid transparent; background:#eee; }

    /* Event feed */
    .log { height: 520px; overflow: auto; border: 1px solid #ddd; padding: 10px; background: #fafafa; border-radius: 12px; }

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

    /* JSON viewer */
    pre { margin: 8px 0 0 0; padding: 10px; background:#0b1020; color:#e7e7e7; border-radius: 10px; overflow:auto; }
    .k { color:#9cdcfe; }      /* keys */
    .s { color:#ce9178; }      /* strings */
    .n { color:#b5cea8; }      /* numbers */
    .b { color:#569cd6; }      /* booleans */
    .null { color:#808080; }   /* null */
    .copyhint { font-size: 11px; color:#888; margin-top:6px; }

    /* Highlight */
    .hit { background: #ff3b3b; color: #fff; padding: 1px 3px; border-radius: 4px; }
    .chip { display:inline-flex; gap:6px; align-items:center; padding: 4px 8px; border:1px solid #ddd; border-radius: 999px; background:#fff; font-size:12px; }
    .chip strong { font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px; }
    .muted { color:#666; font-size:12px; }
    .right { text-align:right; }

    /* Response body viewer */
    .bodybox { height: 240px; overflow:auto; border:1px solid #ddd; border-radius: 12px; padding: 10px; background:#0b1020; color:#e7e7e7; }
    .bodyhdr { display:flex; gap:10px; align-items:center; }
    .bodyhdr button { width:auto; padding: 6px 10px; margin: 0; }
    .bodyhdr .mono { font-size: 12px; }
  </style>
</head>
<body>
  <h2>Arthrex-SAP COE-RE Web Debugger</h2>
  <div class="small mono">CDP: 127.0.0.1:9222 • UI: 127.0.0.1:8765</div>

  <div class="row">
    <div class="col">
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
        <div class="row">
          <div class="col"><button onclick="dbg('pause')">Pause</button></div>
          <div class="col"><button onclick="dbg('resume')">Resume</button></div>
        </div>
        <div class="row">
          <div class="col"><button onclick="dbg('stepOver')">Step Over</button></div>
          <div class="col"><button onclick="dbg('stepInto')">Step Into</button></div>
          <div class="col"><button onclick="dbg('stepOut')">Step Out</button></div>
        </div>
      </div>
    </div>

    <div class="col">
      <div class="split">
        <div class="threads panel">
          <div class="panel-title">Threads (grouped by requestId)</div>
          <div class="small muted">
            Click a thread to filter the Live Feed. Use Trace to highlight keys/values across the filtered events.
          </div>
          <button onclick="clearThreadFilter()">Clear Thread Filter</button>
          <div class="tlist" id="threads"></div>

          <div style="margin-top:10px;">
            <div class="bodyhdr">
              <div class="panel-title" style="margin:0;">Response Body</div>
              <button onclick="fetchBodyForSelected()">Fetch Body</button>
            </div>
            <div class="small mono" id="bodyMeta"></div>
            <div class="bodybox mono" id="bodyBox">Select a thread and click "Fetch Body".</div>
          </div>
        </div>

        <div class="feed">
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
    </div>
  </div>

<script>
  // -----------------------------------------------------------------------------------
  // Front-end notes (peer review friendly):
  //   - We store all incoming events in EVENTS[] (memory).
  //   - Threads are derived from Network.* events by requestId.
  //   - Selecting a thread filters the event feed to that requestId.
  //   - "Fetch Body" requests server-side CDP call Network.getResponseBody(requestId).
  // -----------------------------------------------------------------------------------

  let ws;
  const TARGET_HINT = "alm.cloud.sap/launchpad";

  // Trace state
  let TRACE = "";
  let traceHits = 0;
  const lastSeen = new Map(); // key -> last value seen

  // Local event store (for re-rendering when filters/trace change)
  const EVENTS = [];           // each item = {type, method, params, _ts}
  let FILTER_REQUEST_ID = "";  // thread filter by requestId
  let SELECTED_REQUEST_ID = ""; // currently selected thread

  // Thread model:
  // THREADS[requestId] = { requestId, url, method, status, mimeType, lastEventTs, count }
  const THREADS = new Map();

  function nowTime() {
    const d = new Date();
    return d.toLocaleTimeString([], {hour12:false});
  }

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

  // -------------------------
  // Trace highlighting helpers
  // -------------------------
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

      // Track "had/has" when tracing an exact key
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

  // ---------------------------------------
  // Thread building (group by requestId)
  // ---------------------------------------
  function extractRequestId(method, params) {
    // Network.* events typically carry requestId at the root
    if (!params) return "";
    if (params.requestId) return params.requestId;
    // Sometimes embedded/other events could carry it elsewhere (not typical)
    return "";
  }

  function upsertThreadFromEvent(evt) {
    const { method, params, _ts } = evt;

    if (!method || !params) return;
    if (!method.startsWith("Network.")) return;

    const rid = extractRequestId(method, params);
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

    // Fill from request event
    if (method === "Network.requestWillBeSent" && params.request) {
      current.url = params.request.url || current.url;
      current.method = params.request.method || current.method;
    }

    // Fill from response event
    if (method === "Network.responseReceived" && params.response) {
      current.status = String(params.response.status ?? current.status);
      current.mimeType = String(params.response.mimeType ?? current.mimeType);
      current.url = params.response.url || current.url;
    }

    // Failure event
    if (method === "Network.loadingFailed") {
      current.status = "FAILED";
    }

    THREADS.set(rid, current);
  }

  function renderThreads() {
    const box = document.getElementById("threads");
    box.innerHTML = "";

    // Sort: most recently updated thread first
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
    renderAll();      // re-render feed with filter
    renderThreads();  // update active styling
  }

  function clearThreadFilter() {
    FILTER_REQUEST_ID = "";
    SELECTED_REQUEST_ID = "";
    document.getElementById("bodyMeta").textContent = "";
    document.getElementById("bodyBox").textContent = "Select a thread and click \"Fetch Body\".";
    renderAll();
    renderThreads();
  }

  // ---------------------------------------
  // Event rendering + filtering
  // ---------------------------------------
  function renderEventCard(evt) {
    traceHits = 0;

    const method = evt.method || "";
    const params = evt.params || {};
    const cat = categoryFromMethod(method);

    // Quick headline for readability
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
            <div style="margin-left:auto" class="right">${hitChip}</div>
          </div>
          ${headlineHtml}
        </summary>
        <pre class="mono">${jsonHtml}</pre>
      </details>
    `;
  }

  function eventPassesFilter(evt) {
    if (!FILTER_REQUEST_ID) return true;
    const rid = extractRequestId(evt.method, evt.params);
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

  // ---------------------------------------
  // Trace controls
  // ---------------------------------------
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

  // ---------------------------------------
  // Response body fetch
  // ---------------------------------------
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
    // payload: { requestId, ok, mimeType, base64Encoded, body, error }
    const meta = document.getElementById("bodyMeta");
    const box = document.getElementById("bodyBox");

    if (!payload.ok) {
      meta.textContent = `Selected requestId: ${payload.requestId} • error`;
      box.textContent = payload.error || "Body not available.";
      return;
    }

    meta.textContent = `Selected requestId: ${payload.requestId} • mimeType: ${payload.mimeType || "?"} • base64: ${payload.base64Encoded ? "true" : "false"}`;

    // If base64, display a note + raw base64 (you can enhance later to decode binary downloads safely)
    if (payload.base64Encoded) {
      box.textContent = payload.body || "";
      return;
    }

    // Pretty print JSON if possible
    const text = payload.body || "";
    try {
      const obj = JSON.parse(text);
      box.textContent = JSON.stringify(obj, null, 2);
    } catch {
      box.textContent = text;
    }
  }

  // ---------------------------------------
  // Tab handling
  // ---------------------------------------
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

    // Reset state for a clean session
    EVENTS.length = 0;
    THREADS.clear();
    FILTER_REQUEST_ID = "";
    SELECTED_REQUEST_ID = "";
    document.getElementById("bodyMeta").textContent = "";
    document.getElementById("bodyBox").textContent = "Select a thread and click \"Fetch Body\".";
    document.getElementById("threads").innerHTML = "";
    document.getElementById("log").innerHTML = "";

    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => {
      ws.send(JSON.stringify({type:'connect', tabId}));
      document.getElementById('status').textContent = 'Connecting...';
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data);

      // Status text updates
      if (msg.type === 'status') {
        document.getElementById('status').textContent = msg.message;
        return;
      }

      // Eval results
      if (msg.type === 'evalResult') {
        document.getElementById('evalOut').value = JSON.stringify(msg.result, null, 2);
        return;
      }

      // Response body results
      if (msg.type === 'bodyResult') {
        showBodyResult(msg);
        return;
      }

      // Streamed CDP events
      if (msg.type === 'event') {
        // Keep an in-memory store so filtering and re-rendering is easy
        msg._ts = Date.now();
        EVENTS.push(msg);

        // Update thread model from network events
        upsertThreadFromEvent(msg);

        // Re-render threads + feed (simple approach; can optimize later if needed)
        renderThreads();
        renderAll();
        return;
      }

      // Any unexpected messages (debug)
      console.log("Unknown message", msg);
    };

    ws.onclose = () => document.getElementById('status').textContent = 'Disconnected';
  }

  // ---------------------------------------
  // Evaluate + Debug controls
  // ---------------------------------------
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
# Server-side state and event routing
# --------------------------------------------------------------------------------------
class AppState:
    """
    Holds global state for:
      - CDP connection
      - Connected UI websocket clients
      - Network request metadata to support body fetching
    """

    def __init__(self):
        self.cdp = CDPClient()
        self.connected = False
        self.ws_clients: List[web.WebSocketResponse] = []

        # Track requestId -> (mimeType, url)
        # Used mainly to show helpful metadata in response body viewer.
        self.request_meta: Dict[str, Tuple[str, str]] = {}


state = AppState()


async def broadcast(msg: Dict[str, Any]) -> None:
    """
    Broadcast a JSON message to all connected UI clients.
    """
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
    """
    Receives CDP events from Chrome and forwards a curated subset to the UI.

    We keep the stream intentionally narrow to reduce noise:
      - Network lifecycle events
      - Console logs + exceptions
      - Debugger pause/resume
    """
    method = evt.get("method", "")
    params = evt.get("params", {}) or {}

    # Keep request metadata for response body viewer
    if method == "Network.responseReceived":
        rid = params.get("requestId")
        resp = params.get("response", {}) or {}
        if rid:
            mime = str(resp.get("mimeType", ""))
            url = str(resp.get("url", ""))
            state.request_meta[rid] = (mime, url)

    # Forward only selected events to UI
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
    """
    List CDP "page" targets so the user can pick the right browser tab.
    """
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
# WebSocket handler (UI <-> Python server)
# --------------------------------------------------------------------------------------
async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    """
    UI connects here. Messages supported:
      - {type:"connect", tabId:"..."}      -> connect CDP to that target
      - {type:"eval", expression:"..."}    -> Runtime.evaluate on the page
      - {type:"debug", action:"pause"...}  -> Debugger.pause/resume/step*
      - {type:"getBody", requestId:"..."}  -> Network.getResponseBody for requestId
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    state.ws_clients.append(ws)

    await ws.send_str(json.dumps({"type": "status", "message": "UI connected. Select a tab and click Connect."}))

    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue

        data = json.loads(msg.data)
        mtype = data.get("type")

        # --------------------
        # Connect to a chosen tab
        # --------------------
        if mtype == "connect":
            tab_id = data.get("tabId")
            tabs = await state.cdp.list_tabs()
            target = next((x for x in tabs if x.get("id") == tab_id), None)

            if not target or not target.get("webSocketDebuggerUrl"):
                await ws.send_str(json.dumps({"type": "status", "message": "Tab not found or missing WS URL."}))
                continue

            # Reset metadata per connection session
            state.request_meta = {}

            # Attach to the target
            await state.cdp.connect(target["webSocketDebuggerUrl"])
            state.cdp.on_event(cdp_event_handler)

            # Enable relevant CDP domains
            await state.cdp.send("Runtime.enable")
            await state.cdp.send("Network.enable")
            await state.cdp.send("Log.enable")
            await state.cdp.send("Debugger.enable")

            # Helpful for deeper context on async call stacks
            await state.cdp.send("Runtime.setAsyncCallStackDepth", {"maxDepth": 32})

            state.connected = True
            await ws.send_str(json.dumps({"type": "status", "message": "Connected. Streaming events."}))

        # --------------------
        # Evaluate JS expressions on the page (get/set globals, etc.)
        # --------------------
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

        # --------------------
        # Debugger control (pause/resume/step)
        # --------------------
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

        # --------------------
        # Fetch response body for a network requestId
        # --------------------
        elif mtype == "getBody":
            if not state.connected:
                await ws.send_str(json.dumps({"type": "bodyResult", "ok": False, "error": "Not connected", "requestId": ""}))
                continue

            request_id = str(data.get("requestId", "")).strip()
            if not request_id:
                await ws.send_str(json.dumps({"type": "bodyResult", "ok": False, "error": "Missing requestId", "requestId": ""}))
                continue

            # Provide mimeType metadata if known
            mime, url = state.request_meta.get(request_id, ("", ""))

            try:
                resp = await state.cdp.send("Network.getResponseBody", {"requestId": request_id})
                # CDP shape: { "id": X, "result": { "body": "...", "base64Encoded": bool } }
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

    # Client disconnected
    if ws in state.ws_clients:
        state.ws_clients.remove(ws)
    return ws


# --------------------------------------------------------------------------------------
# Server start
# --------------------------------------------------------------------------------------
async def run_ui_server() -> None:
    """
    Starts the local web UI server and auto-opens the UI URL in the default browser.
    """
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

    # Keep server alive
    while True:
        await asyncio.sleep(3600)


# --------------------------------------------------------------------------------------
# Main orchestration
# --------------------------------------------------------------------------------------
async def main() -> None:
    """
    End-to-end flow:
      1) Close Chrome
      2) Relaunch Chrome with CDP enabled and open Cloud ALM
      3) Wait for CDP readiness
      4) Start UI server and auto-open it
    """
    print("Closing Chrome...")
    kill_chrome()
    await asyncio.sleep(1.0)

    print("Launching Chrome with remote debugging + Cloud ALM...")
    launch_chrome()

    print("Waiting for CDP...")
    ok = await wait_for_cdp(timeout_sec=20)
    if not ok:
        print(f"ERROR: CDP not reachable at {CDP_HTTP}.")
        print("If Chrome didn't launch, check Chrome path or security tools blocking localhost.")
        sys.exit(1)

    print("Starting UI (and auto-opening it)...")
    await run_ui_server()


if __name__ == "__main__":
    asyncio.run(main())
