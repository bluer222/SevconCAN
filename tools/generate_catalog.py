#!/usr/bin/env python3
"""Generate a searchable, filterable HTML catalog of every Gen4 object.

    python3 tools/generate_catalog.py                  # writes docs/catalog.html
    python3 tools/generate_catalog.py --out /tmp/x.html --eds specs/od/other.dcf

The output is one self-contained file (inline CSS/JS, no network): a
search box plus filters for access, category, login level and
pre-operational-only, with one section per object listing every
sub-index, its data type, limits, default, scaling/unit, enum labels
and access rules. A second tab covers the 245-entry fault table.
Source of truth is the EDS/DCF parsed by ``src/sevcon_can/od.py``.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sevcon_can import datatypes as dt  # noqa: E402
from sevcon_can.od import ObjectDictionary, Variable, load_eds  # noqa: E402
from sevcon_can.tables import GEN4_FAULTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "catalog.html"


def _var_record(var: Variable) -> Dict[str, Any]:
    default = var.default()
    return {
        "index": var.index,
        "sub": var.subindex,
        "obj": var.parent_name or var.name,
        "name": var.name,
        "path": var.path,
        "tag": var.tag,
        "access": var.access,
        "dtype": dt.NAMES.get(var.data_type, f"0x{var.data_type:04X}"),
        "low": var.low,
        "high": var.high,
        "default": None if default is None else (
            default.hex() if isinstance(default, (bytes, bytearray)) else default),
        "scaling": var.scaling,
        "units": var.units,
        "labels": {str(k): v for k, v in var.labels.items()},
        "bitfields": [bf.name for bf in var.bitfields],
        "level": var.access_level,
        "preop": var.preop_only,
        "category": var.category,
        "section": var.section,
        "pdo": var.pdo_mappable,
    }


def collect(od: ObjectDictionary) -> List[Dict[str, Any]]:
    return [_var_record(v) for v in od.variables()]


def collect_faults() -> List[Dict[str, Any]]:
    out = []
    for fid in sorted(GEN4_FAULTS):
        f = GEN4_FAULTS[fid]
        out.append({
            "fid": f"0x{fid:04X}",
            "message": f.message,
            "level": f.level,
            "led": f.led_flashes,
            "display": f.display,
            "type": f.type,
            "description": f.description,
            "action": f.action,
            "aliases": list(f.aliases),
            "sources": list(f.sources),
        })
    return out


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light dark}
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0 auto;max-width:1200px;padding:16px;line-height:1.4}
h1{font-size:1.4em;margin:.2em 0}h2.obj{font-size:1.05em;margin:1.2em 0 .3em}
.stats{color:#666;font-size:.9em;margin-bottom:12px}
.tabs button{padding:6px 14px;margin-right:4px;cursor:pointer}
.tabs button.active{font-weight:bold}
.controls{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.controls input[type=text]{flex:2;min-width:200px;padding:6px}
.controls select,.controls label{padding:6px 2px}
table{border-collapse:collapse;width:100%;font-size:.85em}
th,td{border:1px solid #bbb;padding:3px 6px;text-align:left;vertical-align:top}
th{background:#eee;position:sticky;top:0}tr.objhdr td{background:#dfe9f5;font-weight:bold}
@media(prefers-color-scheme:dark){th{background:#333}tr.objhdr td{background:#2a3a55}}
.mono{font-family:ui-monospace,monospace}.muted{color:#888}.hidden{display:none}
.count{font-size:.85em;color:#666;margin:8px 0}
</style>
</head>
<body>
<h1>__TITLE__</h1>
<div class="stats">__STATS__</div>
<div class="tabs">
<button id="tabObj" class="active" onclick="showTab('obj')">Objects (__NOBJ__ / __NVAR__ entries)</button>
<button id="tabFlt" onclick="showTab('flt')">Faults (__NFAULT__)</button>
</div>
<div id="paneObj">
<div class="controls">
<input type="text" id="q" placeholder="Search name, tag, index (e.g. torque, STATUSWORD, 5100)…" oninput="render()">
<select id="fAccess" onchange="render()">
<option value="">access: any</option><option>ro</option><option>rw</option>
<option>rww</option><option>rwr</option><option>wo</option><option>const</option>
</select>
<select id="fCat" onchange="render()">
<option value="">category: any</option><option>CONFIGURATION</option>
<option>MONITORING</option><option>LOGGING</option>
</select>
<select id="fLevel" onchange="render()">
<option value="">login level: any</option><option value="0">level 0 (none)</option>
<option value="1">1 User</option><option value="2">2 Dealer</option>
<option value="3">3 Production</option><option value="4">4 Engineering</option>
<option value="5">5 SEVCON</option>
</select>
<label><input type="checkbox" id="fPreop" onchange="render()"> pre-op only</label>
<label><input type="checkbox" id="fWritable" onchange="render()"> writable</label>
</div>
<div class="count" id="objCount"></div>
<table><thead><tr>
<th>Index</th><th>Sub</th><th>Name / path</th><th>Access</th><th>Type</th>
<th>Limits</th><th>Default</th><th>Scale / unit</th><th>Labels / rules</th>
</tr></thead><tbody id="objBody"></tbody></table>
</div>
<div id="paneFlt" class="hidden">
<div class="controls">
<input type="text" id="fq" placeholder="Search faults (e.g. throttle, battery, F17003)…" oninput="renderFaults()">
</div>
<div class="count" id="fltCount"></div>
<table><thead><tr>
<th>FID</th><th>Message</th><th>Lvl</th><th>LED</th><th>Display</th><th>Description / action</th>
</tr></thead><tbody id="fltBody"></tbody></table>
</div>
<script>
const VARS = __VARS__;
const FAULTS = __FAULTS__;
function hx(n){return "0x"+n.toString(16).toUpperCase().padStart(4,"0");}
function showTab(w){
 document.getElementById("paneObj").classList.toggle("hidden",w!=="obj");
 document.getElementById("paneFlt").classList.toggle("hidden",w!=="flt");
 document.getElementById("tabObj").classList.toggle("active",w==="obj");
 document.getElementById("tabFlt").classList.toggle("active",w==="flt");
}
function matchVar(v,q){
 if(!q)return true;
 q=q.toLowerCase();
 return (v.path+" "+(v.tag||"")+" "+hx(v.index)+" "+v.index).toLowerCase().includes(q);
}
function render(){
 const q=document.getElementById("q").value;
 const acc=document.getElementById("fAccess").value;
 const cat=document.getElementById("fCat").value;
 const lvl=document.getElementById("fLevel").value;
 const preop=document.getElementById("fPreop").checked;
 const writable=document.getElementById("fWritable").checked;
 const rows=VARS.filter(v=>
  matchVar(v,q)&&(!acc||v.access===acc)&&(!cat||v.category===cat)&&
  (lvl===""||String(v.level??0)===lvl)&&(!preop||v.preop)&&
  (!writable||v.access.includes("w")));
 document.getElementById("objCount").textContent=rows.length+" of "+VARS.length+" entries";
 let out="",lastIdx=-1;
 for(const v of rows){
  if(v.index!==lastIdx){
   out+=`<tr class="objhdr"><td class="mono">${hx(v.index)}</td>`+
    `<td colspan="8">${esc(v.obj)}${v.tag?` <span class="muted mono">${esc(v.tag)}</span>`:""}</td></tr>`;
   lastIdx=v.index;
  }
  const lim=(v.low??"–")+" … "+(v.high??"–");
  const sc=v.scaling?("×"+v.scaling+(v.units?" "+v.units:"")):(v.units||"<span class='muted'>–</span>");
  let rules=Object.entries(v.labels).map(([k,val])=>`${k}=${val}`).join("; ");
  if(v.bitfields.length)rules+=(rules?"; ":"")+"bits: "+v.bitfields.join(", ");
  const extra=[];
  if(v.level)extra.push("login ≥"+v.level);
  if(v.preop)extra.push("pre-op only");
  if(extra.length)rules+=(rules?"; ":"")+extra.join("; ");
  out+=`<tr><td></td><td class="mono">${v.sub}</td><td>${esc(v.name)}</td>`+
   `<td class="mono">${v.access}</td><td>${esc(v.dtype)}</td><td class="mono">${lim}</td>`+
   `<td class="mono">${v.default??"<span class='muted'>–</span>"}</td><td>${sc}</td>`+
   `<td>${esc(rules)}</td></tr>`;
 }
 document.getElementById("objBody").innerHTML=out||`<tr><td colspan="9">No matches.</td></tr>`;
}
function renderFaults(){
 const q=(document.getElementById("fq").value||"").toLowerCase();
 const rows=FAULTS.filter(f=>!q||(f.fid+" "+f.message+" "+f.display+" "+f.description+" "+
  f.type+" "+f.aliases.join(" ")).toLowerCase().includes(q));
 document.getElementById("fltCount").textContent=rows.length+" of "+FAULTS.length+" faults";
 document.getElementById("fltBody").innerHTML=rows.map(f=>
  `<tr><td class="mono">${f.fid}</td><td>${esc(f.message)}${f.aliases.length?`<br><span class="muted">aka: ${esc(f.aliases.join("; "))}</span>`:""}</td>`+
  `<td>${f.level??"–"}</td><td>${f.led??"–"}</td><td class="mono">${esc(f.display)}</td>`+
  `<td>${esc(f.description)}${f.action?`<br><b>Action:</b> ${esc(f.action)}`:""}</td></tr>`
 ).join("")||`<tr><td colspan="6">No matches.</td></tr>`;
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
render();renderFaults();
</script>
</body>
</html>
"""


def render_page(od: ObjectDictionary, title: str) -> str:
    records = collect(od)
    n_obj = len({r["index"] for r in records})
    ro = sum(1 for r in records if r["access"] == "ro")
    faults = collect_faults()
    stats = (f"{n_obj} objects, {len(records)} entries "
             f"({ro} read-only); {len(faults)} faults. "
             f"Source: {html.escape(od.source or 'EDS')}. "
             f"Physical value = raw × scaling.")
    page = PAGE
    page = page.replace("__TITLE__", html.escape(title))
    page = page.replace("__STATS__", stats)
    page = page.replace("__NOBJ__", str(n_obj))
    page = page.replace("__NVAR__", str(len(records)))
    page = page.replace("__NFAULT__", str(len(faults)))
    page = page.replace("__VARS__", json.dumps(records))
    page = page.replace("__FAULTS__", json.dumps(faults))
    return page


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eds", default=None, help="EDS/DCF to catalog (default: bundled Gen4.eds)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output HTML file")
    ap.add_argument("--title", default="Sevcon Gen4 object catalog",
                    help="page title")
    args = ap.parse_args(argv)
    if args.eds:
        od = load_eds(args.eds)
    else:
        from sevcon_can.od import bundled_eds_path, load_eds as _load
        od = _load(bundled_eds_path())
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_page(od, args.title), encoding="utf-8")
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
