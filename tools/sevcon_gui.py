#!/usr/bin/env python3
"""Browser GUI for the Gen4: live values on top, per-value Get/Set table below.

    python3 tools/sevcon_gui.py --sim                    # simulator, no hardware
    python3 tools/sevcon_gui.py --channel can0 --node 0x20
    # then open http://localhost:8080 in a browser

Stdlib only (``http.server`` + a background asyncio agent thread; the
page uses vanilla JS, no CDN). Layout, as requested:

* top panel: streamed values refreshing every 500 ms (NMT state, access
  level, statusword, key measurements, active faults);
* table below: one row per watched object with its last-read box, a
  set-value box, and Get / Set buttons (enabled per the object's access
  type — read-only rows get no Set button and vice versa);
* an add-object form (index + sub-index) so any of the 1340 EDS entries
  can join the table, a login form, and NMT pre-op/operational buttons.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import math
import queue
import sys
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sevcon_can.device import AsyncSevconGen4, TARGET_CONTROLLER  # noqa: E402
from sevcon_can import __version__  # noqa: E402
from sevcon_can.descriptions import describe  # noqa: E402
from sevcon_can.exceptions import SevconError  # noqa: E402
from sevcon_can.nmt import state_name  # noqa: E402
from sevcon_can.od import gen4_od, load_eds  # noqa: E402
from sevcon_can.cli import backend_options  # noqa: E402

#: Default table rows: (index, sub).
DEFAULT_ROWS: List[Tuple[int, int]] = [
    (0x5100, 1), (0x5100, 2), (0x5100, 3), (0x5100, 4),
    (0x6077, 0), (0x6078, 0), (0x6074, 0),
    (0x2721, 0), (0x2790, 1),
    (0x6041, 0), (0x6060, 0), (0x6071, 0),
]


# ---------------------------------------------------------------------------
# Agent: the asyncio device lives on its own thread; HTTP handlers reach it
# via Agent.call(), which schedules coroutines and blocks for the result.
# ---------------------------------------------------------------------------

class Agent:
    def __init__(self, node_id: int, channel: Optional[str] = None,
                 password: Optional[int] = None, sim: bool = False, *,
                 bus_options: Optional[dict] = None, od=None) -> None:
        self.node_id = node_id
        self.channel = channel
        self.password = password
        self.sim = sim
        self.bus_options = bus_options
        self.od = od
        self._stop_event = None
        self._closed_event = None
        self._dump_task = None
        self._dump_result = None
        self._dump_progress = {"state": "idle", "completed": 0, "total": 0}
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.dev: Optional[AsyncSevconGen4] = None
        self._sim: Any = None
        self.rows: List[Tuple[int, int]] = list(DEFAULT_ROWS)
        self._ready = threading.Event()
        self._error: Optional[BaseException] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self, timeout: float = 10.0) -> "Agent":
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("CAN agent did not start")
        if self._error is not None:
            raise self._error
        return self

    def stop(self) -> None:
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(self._ready.clear)
            fut = asyncio.run_coroutine_threadsafe(self._aclose(), self.loop)
            try:
                fut.result(5.0)
            except Exception:
                pass
        self._thread.join(5.0)

    def call(self, fn: Callable, *args: Any, timeout: float = 5.0) -> Any:
        if self.loop is None or self._error is not None:
            raise RuntimeError("agent not running")
        fut = asyncio.run_coroutine_threadsafe(fn(*args), self.loop)
        return fut.result(timeout)

    # -- runs on the agent thread --
    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as e:  # noqa: BLE001 - surfaced via start()/call()
            self._error = e
            self._ready.set()

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._closed_event = asyncio.Event()
        try:
            if self.sim:
                from sevcon_can.simulator import create_pair
                dev, sim = create_pair(node_id=self.node_id, od=self.od)
                self.dev, self._sim = dev, sim
                await dev.start()
                await sim.start()
            else:
                if self.bus_options:
                    dev = AsyncSevconGen4.from_python_can(
                        self.node_id, od=self.od, **self.bus_options)
                else:
                    if not self.channel:
                        raise ValueError("channel required without --sim")
                    dev = AsyncSevconGen4.from_socketcan(
                        self.channel, self.node_id, od=self.od)
                self.dev = dev
                await dev.start()
            if self.password is not None:
                await self.dev.login(self.password)
            self._ready.set()
            await self._stop_event.wait()
        finally:
            try:
                await self.cancel_dump()
                if self.dev is not None:
                    await self.dev.close()
                if self._sim is not None:
                    await self._sim.close()
            finally:
                self._closed_event.set()

    async def _aclose(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._closed_event is not None:
            await self._closed_event.wait()

    # -- coroutines executed on the agent loop via call() --
    async def snapshot(self) -> Dict[str, Any]:
        assert self.dev is not None
        dev = self.dev
        out: Dict[str, Any] = {"node": dev.node_id, "rows": []}
        try:
            out["nmt"] = state_name(await dev.nmt_state(cached=False) or -1)
        except SevconError as e:
            out["nmt"] = f"error: {e}"
        try:
            out["level"] = await dev.access_level(cached=False)
        except SevconError as e:
            out["level"] = f"error: {e}"
        try:
            info = await dev.status()
            out["status"] = f"0x{info.raw:04X} {info.state}"
        except SevconError as e:
            out["status"] = f"error: {e}"
        try:
            faults = await dev.active_faults()
            out["faults"] = [f"0x{f.fault_id:04X} "
                             f"{f.info.message if f.info else 'unknown'}" for f in faults]
        except SevconError as e:
            out["faults"] = [f"error: {e}"]
        for index, sub in self.rows:
            try:
                reading = await dev.read(index, sub)
            except SevconError as e:
                out["rows"].append({"index": index, "sub": sub, "error": str(e)})
                continue
            out["rows"].append({
                "index": index, "sub": sub,
                "path": reading.variable.path,
                "access": reading.variable.access,
                "unit": reading.variable.units,
                "value": _json_value(reading.physical if not isinstance(
                    reading.physical, float) else round(reading.physical, 4)),
                "label": _json_value(reading.label),
            })
        return out

    async def describe_rows(self) -> List[Dict[str, Any]]:
        """Static metadata for the table (names, access, units, accepted values)."""
        assert self.dev is not None
        meta = []
        for index, sub in self.rows:
            try:
                var = self.dev.variable((index, sub))
                meta.append(_variable_metadata(var))
            except SevconError as e:
                meta.append({"index": index, "sub": sub, "error": str(e)})
        return meta

    async def search_objects(self, query: str) -> List[Dict[str, Any]]:
        """Search metadata only; this does not send CAN frames."""
        assert self.dev is not None
        return [_variable_metadata(var) for var in self.dev.search(query)]

    async def start_dump(self) -> Dict[str, Any]:
        if self._dump_task is not None and not self._dump_task.done():
            return dict(self._dump_progress)
        assert self.dev is not None
        variables = list(self.dev.od.variables())
        self._dump_result = {
            "schema": "sevcon-can.values.v1", "software_version": __version__,
            "node_id": self.node_id, "simulated": self.sim,
            "target": TARGET_CONTROLLER, "started_at": _utc_now(),
            "dictionary": {"source": self.dev.od.source,
                           "device_info": dict(self.dev.od.device_info)},
            "objects": [], "complete": False,
        }
        self._dump_progress = {"state": "running", "completed": 0,
                               "total": len(variables), "errors": 0, "skipped": 0}
        self._dump_task = asyncio.create_task(self._capture_dump(variables))
        return dict(self._dump_progress)

    async def dump_status(self) -> Dict[str, Any]:
        return dict(self._dump_progress)

    async def cancel_dump(self) -> Dict[str, Any]:
        if self._dump_task is not None and not self._dump_task.done():
            self._dump_task.cancel()
            try:
                await self._dump_task
            except asyncio.CancelledError:
                pass
            # The task can be cancelled before its coroutine starts.
            if self._dump_progress["state"] == "running":
                self._dump_progress["state"] = "cancelled"
                self._dump_result["finished_at"] = _utc_now()
                self._dump_result["summary"] = dict(self._dump_progress)
        return dict(self._dump_progress)

    async def download_dump(self) -> Dict[str, Any]:
        if self._dump_result is None or self._dump_progress["state"] == "running":
            raise ValueError("no finished dump is available")
        return self._dump_result

    async def _capture_dump(self, variables) -> None:
        assert self.dev is not None
        result = self._dump_result
        try:
            result["identity_check"] = _json_value(await self.dev.validate_identity())
            for var in variables:
                entry = {"index": var.index, "subindex": var.subindex,
                         "address": f"{var.index:04X}:{var.subindex:02X}",
                         "path": var.path, "access": var.access,
                         "data_type": var.data_type, "unit": var.units,
                         "scaling": var.scaling, "category": var.category,
                         "eeprom": var.eeprom, "read_at": _utc_now()}
                if not var.readable:
                    entry["status"] = "write_only"
                    self._dump_progress["skipped"] += 1
                else:
                    try:
                        data = await self.dev.read_raw(var.index, var.subindex)
                        entry["raw_hex"] = data.hex()
                        raw = var.decode_raw(data)
                        entry.update(status="ok", raw=_json_value(raw),
                                     physical=_json_value(var.to_physical(raw)),
                                     label=_json_value(var.describe(raw)))
                    except Exception as error:
                        entry.update(status="error", error=f"{type(error).__name__}: {error}")
                        self._dump_progress["errors"] += 1
                result["objects"].append(entry)
                self._dump_progress["completed"] += 1
                await asyncio.sleep(0)
            result["complete"] = True
            self._dump_progress["state"] = "done"
        except asyncio.CancelledError:
            self._dump_progress["state"] = "cancelled"
            raise
        except Exception as error:
            result["error"] = f"{type(error).__name__}: {error}"
            self._dump_progress["state"] = "failed"
        finally:
            result["finished_at"] = _utc_now()
            result["summary"] = dict(self._dump_progress)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value):
    if isinstance(value, bytes):
        return {"hex": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _variable_metadata(var) -> Dict[str, Any]:
    from sevcon_can.descriptions import accepts_text, source_text
    return {"index": var.index, "sub": var.subindex, "path": var.path,
            "access": var.access, "unit": var.units,
            "labels": sorted(var.labels.values()), "accepts": accepts_text(var),
            "required_auth_level": var.access_level, "preop_only": var.preop_only,
            "extra": dict(var.extra), "provenance": source_text(var)}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sevcon Gen4</title>
<style>
body{font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:12px}
#live{background:#f4f6fa;border:1px solid #bbb;padding:10px;display:flex;flex-wrap:wrap;gap:6px 22px}
#live b{margin-right:4px}#faults{color:#a00}
table{border-collapse:collapse;width:100%;font-size:.88em;margin-top:12px}
th,td{border:1px solid #bbb;padding:4px 6px;text-align:left}
th{background:#eee}input[type=text]{width:110px}input.num{width:70px}
td.val{font-family:ui-monospace,monospace;min-width:130px}
button{cursor:pointer}.err{color:#a00}.ok{color:#070}
.forms{display:flex;flex-wrap:wrap;gap:16px;margin-top:12px;align-items:end}
form{border:1px solid #bbb;padding:8px}
#searchF{margin-top:16px}#searchF input{width:min(60vw,480px)}
</style></head><body>
<h2>Sevcon Gen4 <span class="muted">node __NODE__</span></h2>
<div id="live">connecting…</div>
<p><button id="dumpB" type="button">Dump all values</button>
<button id="cancelDumpB" type="button" hidden>Cancel dump</button>
<a id="dumpLink" href="/api/dump/download" hidden>Download JSON</a>
<span id="dumpStatus" role="status"></span></p>
<table><thead><tr><th>Object</th><th>Access</th><th>Accepts</th><th>Last read</th>
<th>Value to set</th><th></th><th></th><th>Status</th><th></th></tr></thead>
<tbody id="rows"></tbody></table>
<div id="detail" style="margin-top:10px;border:1px solid #bbb;padding:8px;min-height:2em">
<span class="muted">Click <b>?</b> on any row to explain that object here.</span></div>
<div class="forms">
<form id="addF"><b>Add object</b><br>
index <input class="num" name="index" placeholder="0x5100" required>
sub <input class="num" name="sub" placeholder="1" required>
<button>Add</button></form>
<form id="loginF"><b>Login</b><br>
password <input class="num" name="password" placeholder="0x4BDF" required>
<button>Login</button> <button type="button" id="logoutB">Logout</button></form>
<form id="nmtF"><b>NMT</b><br>
<button name="how" value="preop">Pre-op</button>
<button name="how" value="op">Operational</button></form>
</div>
<div id="msg"></div>
<form id="searchF"><label for="objectQuery">Find parameter by name, address, tag or bit field</label><br>
<input type="text" id="objectQuery" name="query" placeholder="creep, torque, 291F or STATUSWORD">
<button>Search</button> <button type="button" id="allObjectsB">List all parameters</button>
<span id="searchStatus" role="status"></span></form>
<div id="searchResults"></div>
<script>
const rows=__ROWS__;
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function build(){
 document.getElementById("rows").innerHTML=rows.map((r,i)=>{
  const ro=r.access==="ro"||r.access==="const", wo=r.access==="wo";
  return `<tr><td>${esc(r.path||("0x"+r.index.toString(16)+":"+r.sub))}`+
  `<br><span class="muted">0x${r.index.toString(16)}:${r.sub} ${r.unit?esc(r.unit):""}</span>`+
  `${r.provenance?`<br><span class="muted">${esc(r.provenance)}</span>`:""}</td>`+
  `<td>${r.access||"?"}</td>`+
  `<td class="muted">${esc(r.accepts||"–")}</td>`+
  `<td class="val" id="last${i}">–</td>`+
  `<td>${wo||!ro?`<input type="text" id="set${i}" placeholder="${esc((r.labels||[])[0]||"value…")}">`:"–"}</td>`+
  `<td>${!wo?`<button onclick="doGet(${i})">Get</button>`:""}</td>`+
  `<td>${!ro?`<button onclick="doSet(${i})">Set</button>`:""}</td>`+
  `<td id="st${i}"></td><td><button onclick="showInfo(${i})" title="What is this?">?</button></td></tr>`;}).join("");
 }
async function showInfo(i){
 const r=rows[i],box=document.getElementById("detail");
 box.textContent="…";
 try{const d=await api("/api/describe",{index:r.index,sub:r.sub});
  box.textContent=d.error||d.text;
 }catch(e){box.textContent="error: "+e;}
}
async function api(path,body){
 const r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},
  body:body===undefined?null:JSON.stringify(body)});
 return r.json();
}
async function doGet(i){
 const r=rows[i],st=document.getElementById("st"+i);
 try{const d=await api("/api/get",{index:r.index,sub:r.sub});
  if(d.error)throw d.error;
  document.getElementById("last"+i).textContent=fmt(d);
  st.innerHTML='<span class="ok">ok</span>';
 }catch(e){st.innerHTML='<span class="err">'+esc(e)+"</span>";}
}
async function doSet(i){
 const r=rows[i],st=document.getElementById("st"+i);
 const v=document.getElementById("set"+i).value;
 try{const d=await api("/api/set",{index:r.index,sub:r.sub,value:v});
  if(d.error)throw d.error;
  document.getElementById("last"+i).textContent=fmt(d);
  st.innerHTML='<span class="ok">ok</span>';
 }catch(e){st.innerHTML='<span class="err">'+esc(e)+"</span>";}
}
function fmt(d){
 const value=d.value&&typeof d.value==="object"&&"hex" in d.value?"hex:"+d.value.hex:d.value;
 const label=d.label&&typeof d.label==="object"?JSON.stringify(d.label):d.label;
 return value+(d.unit?" "+d.unit:"")+(label?" ("+label+")":"");
}
async function findObjects(query){
 const status=document.getElementById("searchStatus"),box=document.getElementById("searchResults");
 status.textContent="Searching…";
 try{const d=await api("/api/search",{query});if(d.error)throw Error(d.error);
  status.textContent=d.length+" parameters";
  box.innerHTML=d.length?`<table><thead><tr><th>Parameter</th><th>Access and requirements</th><th>Accepts</th><th></th></tr></thead><tbody>`+
   d.map(r=>`<tr><td>${esc(r.path)}<br><span class="muted">0x${r.index.toString(16)}:${r.sub}</span>`+
    `${r.provenance?`<br><span class="muted">${esc(r.provenance)}</span>`:""}</td>`+
    `<td>${esc(r.access)}${r.required_auth_level!==null?"; login ≥ "+r.required_auth_level:""}${r.preop_only?"; pre-op only":""}</td>`+
    `<td>${esc(r.accepts)}</td><td><button type="button" onclick="addFound(${r.index},${r.sub})">Watch</button></td></tr>`).join("")+`</tbody></table>`:"";
 }catch(e){status.textContent=e.message;}
}
async function addFound(index,sub){
 const d=await api("/api/add",{index,sub});
 if(d.error)document.getElementById("msg").textContent=d.error;else location.reload();
}
document.getElementById("searchF").onsubmit=e=>{e.preventDefault();findObjects(document.getElementById("objectQuery").value);};
document.getElementById("allObjectsB").onclick=()=>{document.getElementById("objectQuery").value="";findObjects("");};
async function live(){
 try{const d=await(await fetch("/api/live")).json();
  document.getElementById("live").innerHTML=
   `<span><b>NMT</b> ${esc(d.nmt)}</span><span><b>Level</b> ${esc(d.level)}</span>`+
   `<span><b>Status</b> ${esc(d.status)}</span>`+
   `<span id="faults"><b>Faults</b> ${esc(d.faults.length?d.faults.join("; "):"none")}</span>`;
  const byKey={};d.rows.forEach(r=>byKey[r.index+":"+r.sub]=r);
  rows.forEach((r,i)=>{const v=byKey[r.index+":"+r.sub];if(!v)return;
   document.getElementById("last"+i).textContent=v.error?("error: "+v.error):fmt(v);});
 }catch(e){document.getElementById("live").textContent="connection error: "+e;}
}
document.getElementById("addF").onsubmit=async e=>{e.preventDefault();
 const f=new FormData(e.target);
 const d=await api("/api/add",{index:f.get("index"),sub:f.get("sub")});
 document.getElementById("msg").textContent=d.error||"added — reload to show";
 if(!d.error)location.reload();};
document.getElementById("loginF").onsubmit=async e=>{e.preventDefault();
 const f=new FormData(e.target);
 const d=await api("/api/login",{password:f.get("password")});
 document.getElementById("msg").textContent=d.error||("level "+d.level);};
document.getElementById("logoutB").onclick=async()=>{
 const d=await api("/api/logout");document.getElementById("msg").textContent=d.error||"logged out";};
document.getElementById("nmtF").onsubmit=async e=>{e.preventDefault();
 const d=await api("/api/nmt",{how:e.submitter.value});
 document.getElementById("msg").textContent=d.error||("NMT "+d.state);};
let dumpTimer=null;
async function dumpStatus(){
 const d=await api("/api/dump/status");
 if(d.error)throw Error(d.error);
 const running=d.state==="running";
 document.getElementById("dumpB").disabled=running;
 document.getElementById("cancelDumpB").hidden=!running;
 document.getElementById("dumpStatus").textContent=
  `${d.state}: ${d.completed}/${d.total}, ${d.errors||0} errors, ${d.skipped||0} write-only`;
 const link=document.getElementById("dumpLink");
 link.hidden=running||d.state==="idle";
 if(!running&&dumpTimer){clearInterval(dumpTimer);dumpTimer=null;}
}
document.getElementById("dumpB").onclick=async()=>{
 try{const d=await api("/api/dump/start");if(d.error)throw Error(d.error);
  await dumpStatus();
  if(d.state==="running"&&!dumpTimer)dumpTimer=setInterval(()=>dumpStatus().catch(e=>{
   document.getElementById("dumpStatus").textContent=e.message;
  }),500);
 }catch(e){document.getElementById("dumpStatus").textContent=e.message;}
};
document.getElementById("cancelDumpB").onclick=async()=>{
 await api("/api/dump/cancel");await dumpStatus();
};
dumpStatus().then(()=>{if(document.getElementById("dumpB").disabled&&!dumpTimer)
 dumpTimer=setInterval(()=>dumpStatus().catch(()=>{}),500);}).catch(()=>{});
build();live();setInterval(live,500);
</script></body></html>
"""


def _reading_json(dev: AsyncSevconGen4, reading) -> Dict[str, Any]:
    value = reading.physical
    if isinstance(value, float):
        value = round(value, 4)
    return {"value": _json_value(value), "unit": reading.variable.units,
            "label": _json_value(reading.label),
            "path": reading.variable.path}


def make_handler(agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sevcon-gui/0.1"

        def log_message(self, *args):  # quiet
            pass

        def _json(self, obj: Any, code: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return {}

        def do_GET(self) -> None:
            if self.path == "/api/live":
                return self._json(agent.call(agent.snapshot))
            if self.path == "/api/dump/download":
                try:
                    dump = agent.call(agent.download_dump)
                except ValueError as error:
                    return self._json({"error": str(error)}, 409)
                body = json.dumps(dump, indent=2, allow_nan=False).encode()
                stamp = dump["started_at"].replace(":", "").replace("+", "_")
                name = f"sevcon-node-{agent.node_id:02X}-{stamp}.json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path != "/":
                self.send_error(404)
                return
            rows = agent.call(agent.describe_rows)
            page = PAGE.replace("__NODE__", f"{agent.node_id:#04x}")
            page = page.replace("__ROWS__", json.dumps(rows))
            body = page.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            assert agent.dev is not None
            dev = agent.dev
            try:
                dump_actions = {"/api/dump/start": agent.start_dump,
                                "/api/dump/status": agent.dump_status,
                                "/api/dump/cancel": agent.cancel_dump}
                if self.path in dump_actions:
                    return self._json(agent.call(dump_actions[self.path]))
                if self.path == "/api/live":
                    return self._json(agent.call(agent.snapshot))
                data = self._body()
                if self.path == "/api/search":
                    return self._json(agent.call(agent.search_objects, str(data.get("query", ""))))
                if self.path == "/api/get":
                    index, sub = int(str(data["index"]), 0), int(str(data["sub"]), 0)
                    return self._json(_reading_json(
                        dev, agent.call(dev.read, index, sub)))
                if self.path == "/api/set":
                    index, sub = int(str(data["index"]), 0), int(str(data["sub"]), 0)
                    return self._json(_reading_json(
                        dev, agent.call(dev.write, index, sub, str(data["value"]))))
                if self.path == "/api/add":
                    index, sub = int(str(data["index"]), 0), int(str(data["sub"]), 0)
                    dev.variable((index, sub))  # validates; raises if unknown
                    if (index, sub) not in agent.rows:
                        agent.rows.append((index, sub))
                    return self._json({"ok": True})
                if self.path == "/api/login":
                    return self._json({"level": agent.call(
                        dev.login, int(str(data["password"]), 0))})
                if self.path == "/api/logout":
                    return self._json({"level": agent.call(dev.logout)})
                if self.path == "/api/nmt":
                    how = str(data.get("how"))
                    state = agent.call(
                        dev.goto_preop if how == "preop" else dev.goto_operational)
                    return self._json({"state": state_name(state if state is not None else -1)})
                if self.path == "/api/describe":
                    from sevcon_can.descriptions import accepts_text, source_text
                    index = int(str(data["index"]), 0)
                    sub = data.get("sub")
                    subindex = int(str(sub), 0) if sub not in (None, "") else None
                    text = describe(index, subindex)
                    try:
                        var = dev.variable(index, subindex)
                    except SevconError:
                        pass
                    else:
                        text += f"\nAccepts: {accepts_text(var)}"
                        if source_text(var):
                            text += f"\n{source_text(var)}"
                    return self._json({"text": text})
                self.send_error(404)
            except (SevconError, ValueError, KeyError) as e:
                self._json({"error": str(e)})
            except Exception as e:  # noqa: BLE001 - reported to the page
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    return Handler


def start_server(agent: Agent, host: str = "127.0.0.1", port: int = 8080):
    server = ThreadingHTTPServer((host, port), make_handler(agent))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="can0")
    ap.add_argument("--backend", default="socketcan", choices=["socketcan", "gs_usb", "slcan"])
    ap.add_argument("--can-port", help="CAN adapter serial port, e.g. /dev/ttyACM0 or COM3")
    ap.add_argument("--bitrate", type=int, default=1000000)
    ap.add_argument("--usb-index", type=int, default=0)
    ap.add_argument("--usb-bus", type=int)
    ap.add_argument("--usb-address", type=int)
    ap.add_argument("--serial-baudrate", type=int, default=115200)
    dictionary = ap.add_mutually_exclusive_group()
    dictionary.add_argument("--eds", help="controller-specific EDS/DCF")
    dictionary.add_argument("--supplemental", action="store_true",
                            help="include optional parameters from other documented firmware dictionaries")
    ap.add_argument("--node", type=lambda s: int(s, 0), default=0x20)
    ap.add_argument("--password", type=lambda s: int(s, 0), default=None)
    ap.add_argument("--sim", action="store_true", help="simulated controller")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-browser", action="store_true", help="do not auto-open a browser")
    args = ap.parse_args(argv)
    options = None
    if not args.sim and args.backend != "socketcan":
        can_args = argparse.Namespace(**vars(args))
        can_args.port = args.can_port
        try:
            options = backend_options(can_args)
        except ValueError as error:
            ap.error(str(error))
    od = (load_eds(args.eds) if args.eds else
          gen4_od(include_supplemental=True) if args.supplemental else None)
    agent = Agent(args.node, channel=args.channel, password=args.password,
                  sim=args.sim, bus_options=options, od=od).start()
    server, _ = start_server(agent, args.host, args.port)
    url = f"http://{args.host}:{args.port}/"
    print(f"serving {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        threading.Event().wait()  # until Ctrl-C
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        agent.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
