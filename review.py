#!/usr/bin/env python3
"""Side-by-side review of a redaction run: source on the left, output on the right.

    python3 review.py                       # asks for the two locations in the browser
    python3 review.py --left A --right B    # skips the setup screen

Either side may be a folder, a .zip, or an s3:// prefix. Verdicts persist to
marks.json keyed by the pair of locations, so closing the tab and coming back
tomorrow resumes where you stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import mimetypes
import re
import socketserver
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import pairing
import render
import stores

HERE = Path(__file__).resolve().parent
MARKS = HERE / "marks.json"
RECENT = HERE / "recent.json"

S: dict = {"ready": False}
RENDER = render.Renderer()
_LOCK = threading.Lock()


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001 -- a damaged sidecar must not lose a session
        return default


def _write_json(p: Path, data) -> None:
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(p)


#: Tokens too generic for "what changed" to be interesting.
_SKIP_TOKEN = re.compile(r"^(?:\d{1,2}|[A-Za-z]{1,2})$")


def _tokens(text: str) -> set[str]:
    return {
        t for t in re.split(r"[^A-Za-z0-9@._+-]+", text)
        if len(t) > 2 and not _SKIP_TOKEN.match(t)
    }


def _doc_text(side: str, sid: str, key: str, store) -> str:
    data = store.cached_read(key)
    if key.lower().endswith(".pdf") and RENDER.available:
        return RENDER.text(f"{side}:{sid}", data)
    return data.decode("utf-8", "replace")


def metrics(sid: str) -> dict:
    """What actually changed between the two sides of one pair.

    Removed and added token sets are the whole job in two numbers: a redaction
    that removed nothing did not run, and one that added nothing substituted
    nothing. Computed on demand, for the open document only — extracting text
    from both sides of a whole batch up front would cost minutes and be thrown
    away.
    """
    row = S["rows"][int(sid)]
    ls, rs = S["left_store"], S["right_store"]
    out = {"how": row["how"], "label": row["label"]}

    src = ls.cached_read(row["left"])
    out["left_bytes"] = len(src)
    if not row["right"]:
        out["missing"] = True
        return out

    dst = rs.cached_read(row["right"])
    out["right_bytes"] = len(dst)
    # Byte-identical output is reported as a fact here, not as a verdict: a
    # document with no PII in it is legitimately unchanged.
    out["identical"] = src == dst

    try:
        a = _doc_text("left", sid, row["left"], ls)
        b = _doc_text("right", sid, row["right"], rs)
    except Exception as exc:  # noqa: BLE001
        out["text_error"] = str(exc)[:200]
        return out

    ta, tb = _tokens(a), _tokens(b)
    removed, added = sorted(ta - tb), sorted(tb - ta)
    out.update(
        left_chars=len(a), right_chars=len(b),
        removed=len(removed), added=len(added),
        removed_sample=removed[:14], added_sample=added[:14],
    )
    if RENDER.available and row["left"].lower().endswith(".pdf"):
        try:
            out["left_pages"] = len(RENDER.pages(f"left:{sid}", src))
            out["right_pages"] = len(RENDER.pages(f"right:{sid}", dst))
        except Exception:  # noqa: BLE001
            pass
    return out


def _migrate(marks: dict) -> dict:
    """Bring older mark files up to the current record shape.

    Verdicts were a bare string, then ``{"v": ..., "note": ...}``. Both become
    a reviewed record with the note kept as the first comment, so nobody loses
    a session to a format change.
    """
    out = {}
    for k, v in marks.items():
        if isinstance(v, str):
            out[k] = {"viewed": True, "reviewed": True, "comments": []}
        elif isinstance(v, dict) and "v" in v:
            note = v.get("note") or ""
            out[k] = {"viewed": True, "reviewed": True,
                      "comments": [note] if note else []}
        elif isinstance(v, dict):
            out[k] = {"viewed": bool(v.get("viewed")),
                      "reviewed": bool(v.get("reviewed")),
                      "comments": list(v.get("comments") or [])}
    return out


def session_id(left: str, right: str) -> str:
    """Stable id for a (left, right) pair, so verdicts survive a re-open."""
    h = hashlib.sha256(f"{left}\x00{right}".encode()).hexdigest()[:12]
    return f"{Path(right.rstrip('/')).name or right}-{h}"


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Review</title><style>
:root{--fg:#1a1d21;--mut:#6b7076;--line:#e4e7ea;--soft:#f7f8f9;--ok:#0b6b5e;--warn:#a15c00;--bad:#b3261e;--sel:#eef4f3}
html{color-scheme:light}*{box-sizing:border-box}
body{margin:0;height:100vh;display:flex;flex-direction:column;background:#fff;color:var(--fg);
 font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Inter,Roboto,sans-serif}
button{font:inherit}

/* popup */
#veil{position:fixed;inset:0;background:rgba(26,29,33,.34);display:none;place-items:center;z-index:60;padding:20px}
#veil.on{display:grid}
.pop{width:100%;max-width:430px;background:#fff;border-radius:13px;padding:20px 22px 18px;box-shadow:0 18px 50px rgba(0,0,0,.24)}
.pop h1{font-size:16.5px;margin:0 0 3px}.pop p.sub{color:var(--mut);font-size:12.5px;margin:0 0 15px}
.pop label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:600;margin:0 0 5px}
.pop input[type=text],.pop select{width:100%;font:12.5px ui-monospace,Menlo,monospace;padding:8px 10px;
 border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--fg)}
.pop input:focus,.pop select:focus{outline:2px solid #cfe3df;border-color:#9ccec5}
.grow{display:flex;gap:7px}.grow input{flex:1;min-width:0}
.mini{padding:8px 11px;border:1px solid var(--line);border-radius:7px;background:#fff;color:var(--fg);cursor:pointer;font-size:12px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:9px}.row{margin:0 0 13px}
.note{font-size:12px;color:var(--mut);margin:8px 0 0}.note.err{color:var(--bad)}.note.warn{color:var(--warn)}
.recent{font:11.5px ui-monospace,Menlo,monospace;color:var(--mut);cursor:pointer;padding:3px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.recent:hover{color:var(--fg);text-decoration:underline}
.acts{display:flex;gap:8px;align-items:center;margin:16px 0 0}
.go{font-weight:600;padding:8px 18px;border-radius:7px;border:1px solid var(--fg);background:var(--fg);color:#fff;cursor:pointer}
.link{margin-left:auto;font-size:12px;color:var(--mut);cursor:pointer}

/* app */
#app{flex:1;display:none;flex-direction:column;min-height:0}
#banner{display:none;background:#fff5e6;border-bottom:1px solid #e8c893;color:var(--warn);padding:9px 14px;font-size:13px;align-items:center;gap:12px}
#banner.on{display:flex}
#banner button{font-weight:600;font-size:12px;padding:5px 12px;border-radius:6px;border:1px solid var(--warn);background:var(--warn);color:#fff;cursor:pointer}
#banner .x{margin-left:auto;cursor:pointer;opacity:.6;font-size:16px}

header{border-bottom:1px solid var(--line);padding:7px 12px;display:flex;gap:10px;align-items:center;flex:0 0 auto}
.icobtn{border:1px solid var(--line);background:#fff;border-radius:6px;cursor:pointer;padding:4px 7px;color:var(--mut);line-height:1;display:flex;align-items:center}
.icobtn:hover{color:var(--fg);border-color:var(--mut)}
.pos{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap;font-size:13px}
.name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:12.5px ui-monospace,Menlo,monospace}
.tag{font-size:10px;text-transform:uppercase;letter-spacing:.05em;padding:2px 6px;border-radius:4px;
 background:var(--soft);border:1px solid var(--line);color:var(--mut);white-space:nowrap}
.tag.size,.tag.sole,.tag.name{background:#fff5e6;border-color:#e8c893;color:var(--warn)}
.tag.missing{background:#fdecea;border-color:#eab6b1;color:var(--bad)}
#rev{display:flex;align-items:center;gap:6px;padding:5px 12px;border-radius:7px;border:1px solid var(--line);
 background:#fff;color:var(--mut);cursor:pointer;font-size:12.5px;font-weight:600;white-space:nowrap}
#rev:hover{border-color:var(--mut);color:var(--fg)}
#rev.on{background:var(--sel);border-color:#9ccec5;color:var(--ok)}
#cbox{font:12.5px inherit;border:1px solid var(--line);border-radius:7px;padding:5px 10px;width:190px;background:#fff;color:var(--fg)}
#cbox:focus{outline:2px solid #cfe3df;border-color:#9ccec5}
.count{font-size:12px;color:var(--mut);white-space:nowrap;font-variant-numeric:tabular-nums}
#split{font:11px ui-monospace,Menlo,monospace;color:var(--mut);cursor:pointer;border:1px solid var(--line);
 border-radius:5px;padding:3px 8px;white-space:nowrap}
#split:hover{color:var(--fg);border-color:var(--mut)}

#cbar{display:none;gap:7px;flex-wrap:wrap;padding:7px 12px;border-bottom:1px solid var(--line);background:var(--soft)}
#cbar.on{display:flex}
.chip{display:flex;align-items:center;gap:7px;background:#fff;border:1px solid var(--line);border-radius:14px;
 padding:3px 6px 3px 11px;font-size:12.5px;max-width:100%}
.chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip b{cursor:pointer;color:var(--mut);font-weight:400;padding:0 4px;border-radius:50%}
.chip b:hover{color:var(--bad);background:#fdecea}

#body{flex:1;display:grid;grid-template-columns:250px 1fr;min-height:0}
#body.narrow{grid-template-columns:1fr}
#body.narrow #side{display:none}
#body.info{grid-template-columns:250px 1fr 268px}
#body.narrow.info{grid-template-columns:1fr 268px}
#info{display:none;border-left:1px solid var(--line);background:#fcfcfd;overflow:auto;min-height:0;padding:12px 13px 26px}
#body.info #info{display:block}
#info h3{font-size:10px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut);
 margin:16px 0 7px;font-weight:600}
#info h3:first-child{margin-top:0}
#info dl{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;margin:0;font-size:12px}
#info dt{color:var(--mut)}
#info dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
#info .vals{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
#info .v{font:10.5px ui-monospace,Menlo,monospace;background:#fff;border:1px solid var(--line);
 border-radius:4px;padding:2px 5px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#info .v.rm{border-color:#cfe0dc;color:var(--ok)}
#info .v.ad{border-color:#e8d5b8;color:var(--warn)}
#info .warnrow{color:var(--warn)} #info .badrow{color:var(--bad)}
#side{border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0;background:#fcfcfd}
#side .top{padding:8px 9px;border-bottom:1px solid var(--line);display:flex;gap:6px;align-items:center}
#side input{flex:1;min-width:0;font-size:12.5px;padding:5px 9px;border:1px solid var(--line);border-radius:6px;background:#fff;color:var(--fg)}
#list{flex:1;overflow:auto;padding:3px 0 24px}
.fold{padding:6px 9px;cursor:pointer;display:flex;gap:7px;align-items:center;border-left:2px solid transparent}
.fold:hover{background:var(--soft)}
.fold[aria-current=true]{background:var(--sel);border-left-color:var(--ok)}
.fold .ic{flex:0 0 auto;display:flex;color:var(--mut)}
.fold .tx{min-width:0;flex:1}
.fold .n{font:11.5px ui-monospace,Menlo,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fold .m{font-size:10.5px;color:var(--mut);margin-top:1px}
.fold .m .w{color:var(--warn)}
.file{padding:4px 9px 4px 26px;font:11px ui-monospace,Menlo,monospace;cursor:pointer;display:flex;gap:6px;align-items:center;color:var(--mut)}
.file:hover{background:var(--soft);color:var(--fg)}
.file[aria-current=true]{background:var(--sel);color:var(--fg);font-weight:600}
.file .ic{flex:0 0 auto;display:flex;opacity:.55}
.file .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.dot{flex:0 0 auto;width:6px;height:6px;border-radius:50%;background:var(--line)}
.dot.viewed{background:#c9ced3}.dot.reviewed{background:var(--ok)}.dot.missing{background:#e8c893}

main{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);min-height:0}
section{background:#fff;display:flex;flex-direction:column;min-width:0;min-height:0}
h2{margin:0;padding:5px 12px;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--mut);
 background:var(--soft);border-bottom:1px solid var(--line);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
h2.r{color:var(--ok)}
.pane{flex:1;display:flex;min-height:0;overflow:hidden}
.pane iframe{flex:1;width:100%;border:0;min-height:0}
.scroll{flex:1;overflow:auto;background:#eef0f2;padding:10px 0;min-height:0}
.scroll img,.scroll .ph{display:block;margin:0 auto 10px;background:#fff;box-shadow:0 1px 5px rgba(0,0,0,.15)}
.empty{flex:1;display:grid;place-items:center;color:var(--mut);font-size:13px;text-align:center;padding:26px}

footer{border-top:1px solid var(--line);padding:6px 12px;font-size:11.5px;color:var(--mut);
 display:flex;gap:14px;align-items:center;flex-wrap:wrap;flex:0 0 auto}
kbd{font:11px ui-monospace,Menlo,monospace;background:var(--soft);border:1px solid var(--line);
 border-bottom-width:2px;border-radius:4px;padding:1px 5px}
#jump{margin-left:auto;font:11.5px ui-monospace,Menlo,monospace;border:1px solid var(--line);border-radius:5px;padding:3px 7px;width:120px}
@media(max-width:820px){main{grid-template-columns:1fr}}
</style></head><body>

<div id="veil" class="on"><div class="pop">
  <h1>Compare a run</h1>
  <p class="sub">Point at the folder, zip or bucket holding it. Both halves are found inside.</p>
  <div class="row"><label for="root">Location</label>
    <div class="grow"><input type="text" id="root" spellcheck="false" placeholder="s3://bucket/export   or a folder">
      <button class="mini" id="browse">Choose…</button></div>
    <div id="recent"></div></div>
  <div class="row" id="profrow" style="display:none"><label for="profile">AWS profile</label>
    <input type="text" id="profile" spellcheck="false" placeholder="default"></div>
  <div class="row two" id="splitrow" style="display:none">
    <div><label for="src">Source</label><select id="src"></select></div>
    <div><label for="out">Output</label><select id="out"></select></div></div>
  <div id="msg"></div>
  <div class="acts"><button class="go" id="go">Open</button>
    <span class="link" id="twoway">two separate locations →</span></div>
  <div id="pair" style="display:none">
    <div class="row" style="margin-top:14px"><label for="left">Source location</label>
      <div class="grow"><input type="text" id="left" spellcheck="false"><button class="mini" id="bl">Choose…</button></div></div>
    <div class="row"><label for="right">Output location</label>
      <div class="grow"><input type="text" id="right" spellcheck="false"><button class="mini" id="br">Choose…</button></div></div></div>
</div></div>

<div id="app">
<div id="banner"><span id="btext"></span><button id="bfix"></button><span class="x" id="bx">&times;</span></div>
<header>
  <button class="icobtn" id="showside" title="show the list  (s)" style="display:none">&#187;</button>
  <span class="pos" id="pos">–</span>
  <span class="name" id="name"></span>
  <span class="tag" id="how"></span>
  <button id="rev"><span id="revic"></span><span id="revtx">Review</span></button>
  <input id="cbox" placeholder="add a comment…" spellcheck="false">
  <span class="count" id="count"></span>
  <button class="icobtn" id="infobtn" title="details  (i)">i</button>
  <span id="split" title="change what is compared"></span>
</header>
<div id="cbar"></div>
<div id="body">
  <aside id="side">
    <div class="top"><input id="q" placeholder="search files and folders" spellcheck="false">
      <button class="icobtn" id="hideside" title="hide the list  (s)">&#171;</button></div>
    <div id="list"></div>
  </aside>
  <main>
    <section><h2 id="lh">Source</h2><div class="pane" id="lp"></div></section>
    <section><h2 class="r" id="rh">Output</h2><div class="pane" id="rp"></div></section>
  </main>
  <aside id="info"></aside>
</div>
<footer>
  <span><kbd>enter</kbd> review + next · <kbd>r</kbd> review · <kbd>c</kbd> comment</span>
  <span><kbd>&uarr;</kbd><kbd>&darr;</kbd> file · <kbd>&larr;</kbd><kbd>&rarr;</kbd> folder · <kbd>[</kbd><kbd>]</kbd> page</span>
  <span><kbd>a</kbd> <span id="mode">unreviewed only</span></span>
  <span><kbd>s</kbd> list · <kbd>i</kbd> details · <kbd>y</kbd> <span id="syn">sync on</span> · <kbd>?</kbd> keys</span>
  <input id="jump" placeholder="jump # or name">
</footer>
</div>

<script>
const el=id=>document.getElementById(id);
const ICON={
 folder:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M1.9 12.6V3.9c0-.6.4-1 1-1h3l1.3 1.7h6c.5 0 1 .4 1 1v7c0 .5-.5 1-1 1H2.9c-.6 0-1-.5-1-1z"/></svg>',
 open:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M1.9 12.6V3.9c0-.6.4-1 1-1h3l1.3 1.7h6c.5 0 1 .4 1 1v1.2"/><path d="M1.9 12.6l1.8-5.1h11l-1.8 5.1a1 1 0 01-1 .7H2.9a1 1 0 01-1-.7z"/></svg>',
 done:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"><circle cx="8" cy="8" r="6.2"/><path d="M5.3 8.2l1.9 1.9 3.6-4"/></svg>',
 all:'<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2" y="2.4" width="12" height="4.6" rx="1"/><rect x="2" y="9" width="12" height="4.6" rx="1"/></svg>',
 doc:'<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M9.3 1.9H4.3c-.6 0-1 .4-1 1v10.2c0 .6.4 1 1 1h7.4c.6 0 1-.4 1-1V5.2L9.3 1.9z"/><path d="M9.1 2.1v3.3h3.4"/></svg>',
 sheet:'<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2.3" y="2.7" width="11.4" height="10.6" rx="1"/><path d="M2.3 6.2h11.4M6.3 6.2v7.1"/></svg>',
 img:'<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2.3" y="3" width="11.4" height="10" rx="1"/><circle cx="6" cy="6.4" r="1"/><path d="M2.7 11.4l3.2-2.9 3 2.5 1.9-1.6 2.5 2.1"/></svg>',
 tick:'<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M3.4 8.4l3 3 6.2-6.6"/></svg>',
};
function fileIcon(n){const e=(n.split(".").pop()||"").toLowerCase();
 if(["csv","tsv","xlsx","xls","parquet"].includes(e))return ICON.sheet;
 if(["png","jpg","jpeg","gif","webp","bmp","svg"].includes(e))return ICON.img;
 return ICON.doc;}
function esc(t){const d=document.createElement("div");d.textContent=t;return d.innerHTML;}

let ALL=[],VIEW=[],i=0,marks={},onlyNew=true,ENT=null,OPEN=null,SYNC=true,CAN=false;
let inspected=null,twoWay=false;

/* ---------- record: viewed / reviewed / comments ---------- */
const key=p=>p.left;
function rec(p){let m=marks[key(p)];
 if(!m) return {viewed:false,reviewed:false,comments:[]};
 return {viewed:!!m.viewed,reviewed:!!m.reviewed,comments:m.comments||[]};}
function save(p,r){
  if(!r.viewed&&!r.reviewed&&!r.comments.length) delete marks[key(p)]; else marks[key(p)]=r;
  fetch("/api/mark",{method:"POST",body:JSON.stringify({key:key(p),rec:r})});
}

/* ---------- popup ---------- */
function note(h,c){el("msg").innerHTML=h?`<p class="note ${c||""}">${h}</p>`:"";}
function syncProf(){el("profrow").style.display=(el("root").value+el("left").value+el("right").value).includes("s3://")?"":"none";}
["root","left","right"].forEach(id=>el(id).addEventListener("input",()=>{syncProf();
 if(id==="root"){el("splitrow").style.display="none";inspected=null;note("");}}));
async function choose(t){note("Opening the picker…");
 const r=await(await fetch("/api/browse",{method:"POST",body:"{}"})).json();
 note(r.error||"",r.error?"err":""); if(r.path){el(t).value=r.path;syncProf();if(t==="root")inspect();}}
el("browse").onclick=()=>choose("root");el("bl").onclick=()=>choose("left");el("br").onclick=()=>choose("right");
el("twoway").onclick=()=>{twoWay=!twoWay;el("pair").style.display=twoWay?"":"none";
 el("splitrow").style.display="none";el("twoway").textContent=twoWay?"← one location instead":"two separate locations →";
 document.querySelectorAll(".pop .row")[0].style.display=twoWay?"none":"";note("");};
async function inspect(){
  const root=el("root").value.trim(); if(!root)return;
  note("Looking inside…");
  const r=await(await fetch("/api/inspect",{method:"POST",
    body:JSON.stringify({root,profile:el("profile").value})})).json();
  if(r.error){note(r.error,"err");return;}
  inspected=r;
  const fill=(sel,val,blank)=>{sel.innerHTML="";
    (blank?[{name:"",files:0}]:[]).concat(r.options).forEach(o=>{
      const op=document.createElement("option");op.value=o.name;
      op.textContent=o.name?`${o.name}/  (${o.files})`:"everything else";
      if(o.name===val)op.selected=true;sel.appendChild(op);});};
  fill(el("src"),r.source||"",true); fill(el("out"),r.output||"",false);
  el("splitrow").style.display="grid";
  note(r.warn||"Found both halves. Change either if this is not right.",r.warn?"warn":"");
}
el("root").addEventListener("keydown",e=>{if(e.key==="Enter"){inspected?open_():inspect();}});
async function open_(){
  el("go").disabled=true;note("Reading both sides…");
  const body=twoWay?{left:el("left").value,right:el("right").value,profile:el("profile").value}
                   :{root:el("root").value,profile:el("profile").value,source:el("src").value,output:el("out").value};
  let r; try{r=await(await fetch("/api/open",{method:"POST",body:JSON.stringify(body)})).json();}
  catch(e){note("could not reach the server: "+e,"err");el("go").disabled=false;return;}
  el("go").disabled=false;
  if(r.error){note(r.error,"err");return;}
  if(!r.pairs.length){note("nothing paired up — try a different source or output","err");return;}
  start(r);
}
el("go").onclick=()=>{ if(!twoWay&&!inspected) inspect(); else open_(); };

function start(r){
  ALL=r.pairs; marks=r.marks||{};
  el("veil").classList.remove("on"); el("app").style.display="flex";
  el("lh").textContent="Source · "+(r.left_short||"source"); el("lh").title=r.left||"";
  el("rh").textContent="Output · "+(r.right_short||"output"); el("rh").title=r.right||"";
  el("split").textContent=(r.source||"everything else")+" → "+(r.output||"?");
  if(r.hint){el("btext").textContent=r.hint.text;el("bfix").textContent="use "+r.hint.output+"/";
    el("bfix").onclick=()=>{el("out").value=r.hint.output;el("banner").classList.remove("on");open_();};
    el("banner").classList.add("on");}
  else el("banner").classList.remove("on");
  ENT=null;OPEN=null;i=0;
  build(); list();
  const w=parseInt(location.hash.slice(1),10);
  if(!isNaN(w)){const at=VIEW.findIndex(p=>p.id===w); if(at>=0) i=at;}
  render();
}
el("bx").onclick=()=>el("banner").classList.remove("on");
el("split").onclick=()=>{el("app").style.display="none";el("veil").classList.add("on");note("");
  if(!twoWay&&el("root").value.trim())inspect();};

/* ---------- list ---------- */
const entOf=p=>p.label.split("/")[0];
const restOf=p=>p.label.split("/").slice(1).join("/");
function q(){return el("q").value.trim().toLowerCase();}
function matches(p){const s=q(); return !s || p.label.toLowerCase().includes(s);}
function build(){
  let v = ALL.filter(matches);
  if(ENT) v = v.filter(p=>entOf(p)===ENT);
  VIEW = onlyNew ? v.filter(p=>!rec(p).reviewed) : v;
  if(i>=VIEW.length) i=Math.max(0,VIEW.length-1);
}
function list(){
  const box=el("list"); box.innerHTML="";
  const hits=ALL.filter(matches);
  const groups=new Map();
  hits.forEach(p=>{const e=entOf(p); if(!groups.has(e))groups.set(e,[]); groups.get(e).push(p);});
  // A search that matches files opens the folders holding them, so the hits
  // are visible without a second click.
  const auto = q() && groups.size<=12;

  const add=(name,files,isAll)=>{
    let done=0,miss=0;
    files.forEach(p=>{ if(rec(p).reviewed) done++; if(p.how==="missing") miss++; });
    const full = done>=files.length && files.length>0;
    const opened = isAll ? false : (OPEN===name || auto);
    const d=document.createElement("div");
    d.className="fold"; d.setAttribute("aria-current", isAll?(ENT===null):(ENT===name));
    d.innerHTML=`<span class="ic">${isAll?ICON.all:(full?ICON.done:(opened?ICON.open:ICON.folder))}</span>
      <span class="tx"><div class="n">${isAll?"All folders":esc(name)}</div>
      <div class="m">${done}/${files.length}${miss?` · <span class="w">${miss} missing</span>`:""}</div></span>`;
    d.onclick=()=>{
      if(isAll){ENT=null;OPEN=null;}
      // Click toggles: a second click on the open folder closes it again.
      else if(ENT===name && OPEN===name){OPEN=null;}
      else {ENT=name;OPEN=name;}
      i=0;build();list();render();
    };
    box.appendChild(d);
    if(!isAll && opened) files.forEach(p=>{
      const r=rec(p);
      const cls = r.reviewed?"reviewed":(p.how==="missing"?"missing":(r.viewed?"viewed":""));
      const f=document.createElement("div"); f.className="file";
      f.setAttribute("aria-current", !!(VIEW[i]&&VIEW[i].id===p.id));
      f.innerHTML=`<span class="dot ${cls}"></span><span class="ic">${fileIcon(restOf(p))}</span><span class="t">${esc(restOf(p))}</span>`;
      f.title=p.label;
      f.onclick=ev=>{ev.stopPropagation();
        if(onlyNew && rec(p).reviewed){onlyNew=false;el("mode").textContent="all files";}
        ENT=entOf(p); OPEN=ENT; build();
        const at=VIEW.findIndex(x=>x.id===p.id); if(at>=0){i=at;list();render();}};
      box.appendChild(f);
    });
  };
  if(!q()) add("all", ALL, true);
  [...groups.keys()].sort().forEach(n=>add(n,groups.get(n),false));
}
el("q").addEventListener("input",()=>{i=0;build();list();render();});

function folders(){return [...new Set(ALL.filter(matches).map(entOf))].sort();}
function stepFolder(d){
  const ns=folders(); if(!ns.length)return;
  const at=ns.indexOf(ENT);
  const n = at<0 ? (d>0?0:ns.length-1) : Math.min(ns.length-1,Math.max(0,at+d));
  ENT=ns[n]; OPEN=ENT; i=0; build(); list(); render();
  const cur=el("list").querySelector('.fold[aria-current=true]');
  if(cur) cur.scrollIntoView({block:"nearest"});
}
function toggleSide(force){
  const on = force===undefined ? !el("body").classList.contains("narrow") : !force;
  el("body").classList.toggle("narrow",on);
  el("showside").style.display = on?"":"none";
}
el("hideside").onclick=()=>toggleSide(false);
el("showside").onclick=()=>toggleSide(true);

/* ---------- panes ---------- */
let PAGE=1,LS=null,RS=null,BUSY=false,SEQ=0,INFO=false,MSEQ=0;
function iframeFor(side,id){const f=document.createElement("iframe");
 f.src="/doc/"+side+"/"+id+(PAGE>1?"#page="+PAGE:""); return f;}
function build_pane(box,side,id,meta){
  box.innerHTML="";
  if(meta.kind!=="pdf"){box.appendChild(iframeFor(side,id));return null;}
  const sc=document.createElement("div"); sc.className="scroll";
  const W=Math.max(280,(box.clientWidth||600)-24);
  meta.pages.forEach((sz,n)=>{
    const h=Math.round(W*sz[1]/Math.max(1,sz[0]));
    // The image goes into the DOM immediately, sized from the page's real
    // aspect so it holds its space before it loads — which is what keeps the
    // other pane from drifting out of step, and what lets loading="lazy"
    // work at all. Building it detached and swapping it in on load was a
    // deadlock: the browser will not load a lazy image that is not in the
    // document, so a 28-page report rendered as two blank panes.
    const img=new Image(W,h);
    img.style.width=W+"px"; img.style.height=h+"px";
    img.loading = n<2 ? "eager" : "lazy";
    img.decoding="async"; img.alt="";
    img.src="/page/"+side+"/"+id+"/"+n+".png";
    sc.appendChild(img);
  });
  box.appendChild(sc);
  return sc;
}
function linkScroll(a,b){
  if(!a||!b)return;
  const mirror=(from,to)=>()=>{
    if(!SYNC||BUSY)return; BUSY=true;
    const r=Math.max(1,from.scrollHeight-from.clientHeight);
    to.scrollTop=(from.scrollTop/r)*Math.max(1,to.scrollHeight-to.clientHeight);
    requestAnimationFrame(()=>{BUSY=false;});
  };
  a.addEventListener("scroll",mirror(a,b),{passive:true});
  b.addEventListener("scroll",mirror(b,a),{passive:true});
}
function step_page(d){
  const p=VIEW[i]; if(!p)return;
  if(LS){const kids=[...LS.children];
    const n=Math.max(0,Math.min(kids.length-1,PAGE-1+d));
    if(n===PAGE-1)return; PAGE=n+1;
    if(kids[n]) LS.scrollTop=kids[n].offsetTop-LS.offsetTop;
    return;}
  PAGE=Math.max(1,PAGE+d);
  el("lp").innerHTML="";el("lp").appendChild(iframeFor("left",p.id));
  if(p.right){el("rp").innerHTML="";el("rp").appendChild(iframeFor("right",p.id));}
}
async function panes(p){
  const seq=++SEQ; LS=RS=null; PAGE=1;
  let lm={kind:"other"},rm={kind:"none"};
  if(CAN){ try{
    lm=await (await fetch("/api/doc/left/"+p.id)).json();
    if(p.right) rm=await (await fetch("/api/doc/right/"+p.id)).json();
  }catch(e){} }
  if(seq!==SEQ) return;                 // a slower answer for a document already left behind
  LS=build_pane(el("lp"),"left",p.id,lm);
  if(p.right) RS=build_pane(el("rp"),"right",p.id,rm);
  else el("rp").innerHTML="<div class='empty'><b>Nothing in the output for this document.</b><br>"+
        "Either the run withheld it, or it was never processed.</div>";
  linkScroll(LS,RS);
}

/* ---------- details ---------- */
const kb=n=>n==null?"–":(n<1024?n+" B":(n<1048576?(n/1024).toFixed(0)+" KB":(n/1048576).toFixed(1)+" MB"));
const num=n=>n==null?"–":n.toLocaleString();
function dl(rows){return `<dl>${rows.map(([k,v,c])=>
  `<dt>${k}</dt><dd class="${c||""}">${v}</dd>`).join("")}</dl>`;}

function folderStats(name){
  const f=ALL.filter(p=>entOf(p)===name);
  let r=0,c=0,v=0,m=0;
  f.forEach(p=>{const x=rec(p); if(x.reviewed)r++; if(x.viewed||x.reviewed)v++;
    c+=x.comments.length; if(p.how==="missing")m++;});
  return {n:f.length,r,c,v,m};
}
async function details(){
  if(!INFO) return;
  const box=el("info"), p=VIEW[i];
  if(!p){ box.innerHTML="<h3>Nothing selected</h3>"; return; }

  const fs = folderStats(entOf(p));
  let run={n:ALL.length,r:0,c:0,v:0,m:0,f:new Set()};
  ALL.forEach(x=>{const y=rec(x); if(y.reviewed)run.r++; if(y.viewed||y.reviewed)run.v++;
    run.c+=y.comments.length; if(x.how==="missing")run.m++; run.f.add(entOf(x));});

  const tail =
    `<h3>Folder</h3>${dl([["files",num(fs.n)],["reviewed",num(fs.r)],
      ["viewed",num(fs.v)],["comments",num(fs.c)],
      ["missing",num(fs.m),fs.m?"warnrow":""]])}` +
    `<h3>Run</h3>${dl([["folders",num(run.f.size)],["files",num(run.n)],
      ["viewed",num(run.v)],["reviewed",num(run.r)],["comments",num(run.c)],
      ["missing",num(run.m),run.m?"warnrow":""]])}`;

  box.innerHTML = `<h3>Document</h3><div style="color:var(--mut);font-size:12px">loading…</div>` + tail;

  const seq=++MSEQ;
  let m; try{ m=await (await fetch("/api/metrics/"+p.id)).json(); }catch(e){ m={error:String(e)}; }
  if(seq!==MSEQ || !INFO) return;

  let head;
  if(m.error) head = `<div class="badrow" style="font-size:12px">${esc(m.error)}</div>`;
  else if(m.missing) head = dl([["pairing",esc(m.how)],["source",kb(m.left_bytes)],
                                ["output","nothing",'badrow']]);
  else {
    head = dl([
      ["pairing", esc(m.how), (["size","sole","name"].includes(m.how)?"warnrow":"")],
      ["pages", (m.left_pages!=null?`${m.left_pages} → ${m.right_pages}`:"–")],
      ["size", `${kb(m.left_bytes)} → ${kb(m.right_bytes)}`],
      ["text", (m.left_chars!=null?`${num(m.left_chars)} → ${num(m.right_chars)}`:"–")],
      ["identical", m.identical?"yes":"no", m.identical?"warnrow":""],
      ["removed", num(m.removed)],
      ["added", num(m.added)],
    ]);
    if(m.removed_sample&&m.removed_sample.length)
      head += `<h3>Removed from the output</h3><div class="vals">`+
        m.removed_sample.map(v=>`<span class="v rm">${esc(v)}</span>`).join("")+`</div>`;
    if(m.added_sample&&m.added_sample.length)
      head += `<h3>Only in the output</h3><div class="vals">`+
        m.added_sample.map(v=>`<span class="v ad">${esc(v)}</span>`).join("")+`</div>`;
  }
  box.innerHTML = `<h3>Document</h3>${head}` + tail;
}
function toggleInfo(force){
  INFO = force===undefined ? !INFO : !!force;
  el("body").classList.toggle("info",INFO);
  details();
}
el("infobtn").onclick=()=>toggleInfo();

/* ---------- render ---------- */
function counters(){
  let v=0,r=0,c=0;
  ALL.forEach(p=>{const m=rec(p); if(m.viewed||m.reviewed)v++; if(m.reviewed)r++; c+=m.comments.length;});
  el("count").textContent=`${v} viewed · ${r} reviewed · ${ALL.length} files${c?` · ${c} comments`:""}`;
}
function comments(p){
  const r=rec(p), bar=el("cbar");
  bar.innerHTML=""; bar.classList.toggle("on", r.comments.length>0);
  r.comments.forEach((t,n)=>{
    const c=document.createElement("span"); c.className="chip";
    c.innerHTML=`<span>${esc(t)}</span><b title="remove">&times;</b>`;
    c.querySelector("b").onclick=()=>{const q=rec(p); q.comments.splice(n,1); save(p,q); comments(p); counters(); list();};
    bar.appendChild(c);
  });
}
function head(){
  if(!VIEW.length){
    el("pos").textContent="0 / 0"; el("name").textContent="nothing matches";
    el("how").textContent=""; el("rev").classList.remove("on");
    el("cbar").classList.remove("on"); counters(); return false;
  }
  const p=VIEW[i], r=rec(p);
  el("pos").textContent=`${i+1} / ${VIEW.length}`;
  el("name").textContent=p.label; el("name").title=p.label;
  el("how").textContent=p.how; el("how").className="tag "+p.how;
  el("rev").classList.toggle("on",r.reviewed);
  el("revic").innerHTML=r.reviewed?ICON.tick:"";
  el("revtx").textContent=r.reviewed?"Reviewed":"Review";
  comments(p); counters();
  return true;
}
function render(){
  if(!head()){ el("lp").innerHTML=el("rp").innerHTML=
    "<div class='empty'>Nothing here. Press <b>a</b> for all files, or clear the search.</div>"; return; }
  const p=VIEW[i];
  const r=rec(p);
  // Opening a document is what "viewed" means; nothing to press.
  if(!r.viewed){ r.viewed=true; save(p,r); }
  panes(p);
  details();
  location.hash=p.id;
}
let TICK=null;
function go(d){
  if(!VIEW.length)return;
  i=Math.min(VIEW.length-1,Math.max(0,i+d));
  head();                                  // header keeps up with the key
  clearTimeout(TICK);
  // Holding the key used to load every document it passed through. Only the
  // one you stop on is fetched.
  TICK=setTimeout(()=>{ render(); list();
    const cur=el("list").querySelector('.file[aria-current=true]');
    if(cur) cur.scrollIntoView({block:"nearest"});
  },90);
}
function toggleReview(){
  const p=VIEW[i]; if(!p)return;
  const r=rec(p); r.reviewed=!r.reviewed; if(r.reviewed) r.viewed=true;
  save(p,r);
  if(onlyNew && r.reviewed){ build(); list(); render(); } else { head(); list(); details(); }
}
// Review and move on in one key: the same deliberate act, at the cost of just
// stepping past. j/down stays a pure skip for anything you want to come back to.
function reviewNext(){
  const p=VIEW[i]; if(!p) return;
  const r=rec(p); r.reviewed=true; r.viewed=true; save(p,r);
  if(onlyNew){ build(); list(); render(); } else { go(1); list(); }
}
function reviewFolder(){
  const p=VIEW[i]; if(!p) return;
  const name=entOf(p);
  ALL.filter(x=>entOf(x)===name).forEach(x=>{
    const r=rec(x); if(!r.reviewed){ r.reviewed=true; r.viewed=true; save(x,r); }
  });
  build(); list(); render();
}
el("rev").onclick=toggleReview;
el("cbox").addEventListener("keydown",e=>{
  if(e.key!=="Enter")return;
  const p=VIEW[i], t=e.target.value.trim(); if(!p||!t)return;
  const r=rec(p); r.comments=r.comments.concat([t]); r.viewed=true;
  save(p,r); e.target.value=""; comments(p); counters(); list();
});
function jump(s){
  s=(s||"").trim(); if(!s)return;
  const n=parseInt(s,10);
  if(!isNaN(n)&&String(n)===s){i=Math.min(VIEW.length-1,Math.max(0,n-1));render();list();return;}
  const at=VIEW.findIndex(p=>p.label.toLowerCase().includes(s.toLowerCase()));
  if(at>=0){i=at;render();list();}
}
el("jump").addEventListener("keydown",e=>{if(e.key==="Enter"){jump(e.target.value);e.target.blur();}});

addEventListener("keydown",e=>{
  if(el("app").style.display==="none")return;
  if(e.target.tagName==="INPUT"||e.target.tagName==="SELECT"){
    if(e.key==="Escape") e.target.blur();
    return;
  }
  if(e.metaKey||e.ctrlKey||e.altKey)return;
  const k=e.key, kl=k.toLowerCase();
  if(kl==="j"||k==="ArrowDown"||k===" "){e.preventDefault();go(1);}
  else if(kl==="k"||k==="ArrowUp"){e.preventDefault();go(-1);}
  else if(k==="ArrowRight"){e.preventDefault();stepFolder(1);}
  else if(k==="ArrowLeft"){e.preventDefault();stepFolder(-1);}
  else if(k==="]"){e.preventDefault();step_page(1);}
  else if(k==="["){e.preventDefault();step_page(-1);}
  else if(k==="Enter"){e.preventDefault();reviewNext();}
  else if(k==="R"){e.preventDefault();reviewFolder();}
  else if(kl==="r"){e.preventDefault();toggleReview();}
  else if(kl==="c"){e.preventDefault();el("cbox").focus();}
  else if(kl==="s"){e.preventDefault();toggleSide();}
  else if(kl==="i"){e.preventDefault();toggleInfo();}
  else if(kl==="y"){e.preventDefault();SYNC=!SYNC;el("syn").textContent=SYNC?"sync on":"sync off";}
  else if(kl==="a"){e.preventDefault();onlyNew=!onlyNew;
    el("mode").textContent=onlyNew?"unreviewed only":"all files";build();list();render();}
  else if(kl==="e"){e.preventDefault();el("app").style.display="none";
    el("veil").classList.add("on");note("");if(!twoWay&&el("root").value.trim())inspect();}
  else if(k==="/"){e.preventDefault();el("q").focus();}
  else if(k==="?"){e.preventDefault();alert(
   "Move\\n  j / down      next file\\n  k / up        previous file\\n"+
   "  right / left  next / previous folder\\n  ] / [         both panes one page\\n\\n"+
   "Mark\\n  r             reviewed on or off\\n  c             add a comment\\n\\n"+
   "View\\n  a             all files / unreviewed only\\n  s             show or hide the list\\n"+
   "  y             scroll sync on or off\\n  /             search\\n  e             change what is compared");}
});

fetch("/api/boot").then(r=>r.json()).then(b=>{
  CAN=!!b.render;
  const box=el("recent"); box.innerHTML="";
  (b.recent||[]).slice(0,3).forEach(v=>{const d=document.createElement("div");
    d.className="recent";d.textContent="↩ "+v;
    d.onclick=()=>{el("root").value=v;syncProf();inspect();};box.appendChild(d);});
  if(b.ready) start(b); else el("root").focus();
});
</script></body></html>"""


def _sides(root, left, right, profile, source, output):
    """Resolve the request into (left_store, right_store, filters)."""
    if root:
        st = stores.open_store(root, profile)
        # One location: both halves live inside it, told apart by folder name.
        # An empty source means "everything that is not the output", which is
        # the shape of an in-place run (the tree is the source, _pii/output is
        # the result).
        return st, st, {
            "left_only": source or None,
            "right_only": output or None,
            "left_exclude": None if source else (output or None),
        }
    ls, rs = stores.open_store(left, profile), stores.open_store(right, profile)
    # With two explicit locations there is no selector to disambiguate, so a
    # side that holds the same documents twice (a download carrying both
    # "raw/" and "files/") would pair each source against its own sibling.
    # Say so rather than returning a confident, wrong index.
    for side, st in (("source", ls), ("output", rs)):
        dup = pairing.find_variants(st.docs, set(pairing.DEFAULT_IGNORE))
        if len(dup) > 1:
            names = ", ".join(f"{k}/ ({v})" for k, v in sorted(dup.items(), key=lambda t: -t[1]))
            raise ValueError(
                f"the {side} holds more than one copy of each document ({names}). "
                f"Point at one location instead and pick the two halves there, "
                f"or give a more specific path."
            )
    return ls, rs, {}


def inspect_root(root: str, profile: str | None) -> dict:
    """What the two halves inside one location look like."""
    st = stores.open_store(root, profile)
    # Deliberately the RAW listing, not ``docs``: an in-place run puts its
    # output under ``_pii/output``, which the document filter drops. It has to
    # be visible here or it could never be chosen as a side.
    paths = [p for p in st._list() if "__MACOSX" not in p]
    if not paths:
        return {"error": f"nothing found in {root}"}
    return pairing.autosplit(paths)


def open_review(root=None, left=None, right=None, profile=None,
                source=None, output=None, ignore=None) -> dict:
    ls, rs, filt = _sides(root, left, right, profile, source, output)
    ign = {s.strip().lower() for s in (ignore or pairing.DEFAULT_IGNORE) if s.strip()}
    ign |= {s.lower() for s in (source, output) if s}

    idx = pairing.build(ls, rs, ignore=ignore, **filt)

    rows = [
        {"id": n, "left": p["left"], "right": p["right"], "how": p["how"],
         "label": "/".join(pairing.normalise(p["left"], ign))}
        for n, p in enumerate(idx["pairs"])
    ]
    # A source document with nothing opposite it belongs in the review -- that
    # is exactly the case a reviewer must see -- so it is listed with an empty
    # right pane rather than dropped from the count.
    for lp in idx["unmatched_left"]:
        rows.append({"id": len(rows), "left": lp, "right": None, "how": "missing",
                     "label": "/".join(pairing.normalise(lp, ign))})
    rows.sort(key=lambda r: r["label"])
    for n, r in enumerate(rows):
        r["id"] = n

    lname = f"{ls}::{source or ''}"
    rname = f"{rs}::{output or ''}"
    sid = session_id(lname, rname)
    all_marks = _read_json(MARKS, {})
    marks = _migrate(all_marks.setdefault(sid, {}))
    all_marks[sid] = marks

    with _LOCK:
        S.update(ready=True, rows=rows, left_store=ls, right_store=rs,
                 sid=sid, all_marks=all_marks, marks=marks,
                 left=f"{ls}{'/' + source if source else ''}",
                 right=f"{rs}{'/' + output if output else ''}")

    if root or left:
        spec = root or left
        recent = _read_json(RECENT, [])
        recent = [spec] + [x for x in recent if x != spec]
        _write_json(RECENT, recent[:10])

    counts = dict(idx["counts"])
    counts["by_how"] = dict(counts["by_how"])
    counts["by_how"]["missing"] = len(idx["unmatched_left"])
    print(f"  {len(rows)} documents  {counts['by_how']}", flush=True)
    if idx["unmatched_right"]:
        print(f"  {len(idx['unmatched_right'])} output file(s) with no source", flush=True)

    # Safety net. If almost nothing paired, the split is almost certainly
    # wrong — and the reviewer has no way to tell that from a screen of
    # "nothing in the output", which looks exactly like a pipeline that
    # dropped everything. So check whether another folder would do better and
    # offer it, rather than letting a wrong choice read as a catastrophic run.
    hint = None
    missing = len(idx["unmatched_left"])
    if root and rows and missing / len(rows) > 0.5:
        guess = pairing.autosplit([p for p in ls.paths if "__MACOSX" not in p])
        better = [o for o in guess["options"]
                  if o["name"] not in (source, output) and o["files"] > missing * 0.5]
        if better:
            alt = max(better, key=lambda o: o["files"])["name"]
            hint = {"output": alt,
                    "text": (f"{missing} of {len(rows)} documents have no counterpart in "
                             f"{output or 'the output'}/. {alt}/ holds far more — "
                             f"that is probably the output folder.")}

    def short(store, folder):
        # The header had the whole absolute path in it, which is the one part
        # of the screen that never changes and the least worth reading.
        return folder or Path(str(store).split(":", 1)[-1].rstrip("/")).name or str(store)

    session = {"pairs": rows, "marks": marks, "counts": counts,
               "left": S["left"], "right": S["right"], "hint": hint,
               "left_short": short(ls, source), "right_short": short(rs, output),
               "source": source, "output": output, "root": root}
    with _LOCK:
        S["session"] = session
    return session


def native_picker() -> dict:
    """Ask macOS for a folder or file. Falls back to a clear message."""
    import subprocess

    script = (
        'try\n'
        '  set f to choose folder with prompt "Pick the run to review"\n'
        '  return POSIX path of f\n'
        'on error number -128\n'
        '  return ""\n'
        'end try'
    )
    try:
        out = subprocess.run(["osascript", "-e", script],
                             capture_output=True, timeout=180)
    except FileNotFoundError:
        return {"error": "no native picker here — paste the path instead"}
    except subprocess.TimeoutExpired:
        return {"error": "picker timed out"}
    if out.returncode != 0:
        return {"error": out.stderr.decode()[:200] or "picker failed"}
    return {"path": out.stdout.decode().strip().rstrip("/")}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body: bytes, ctype: str, code: int = 200, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(json.dumps(obj).encode(), "application/json", code)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._send(PAGE.encode(), "text/html; charset=utf-8")
        if path == "/api/boot":
            out = {"recent": _read_json(RECENT, []),
                   "ready": S.get("ready", False),
                   "left": S.get("left", ""), "right": S.get("right", "")}
            if S.get("ready"):
                # The full session, not just the pairs. Returning less meant a
                # page reload came back with no idea what it was comparing:
                # the panes read "Source · source" and the split chip showed a
                # question mark.
                out.update(S.get("session", {}))
            out["render"] = RENDER.available
            return self._json(out)
        if path.startswith("/api/metrics/"):
            try:
                return self._json(metrics(path.rsplit("/", 1)[1]))
            except Exception as exc:  # noqa: BLE001 -- advisory panel
                return self._json({"error": f"{type(exc).__name__}: {exc}"})
        if path.startswith("/api/doc/"):
            try:
                _, _, _, side, sid = path.split("/", 4)
                row = S["rows"][int(sid)]
                key = row["left"] if side == "left" else row["right"]
                if key is None:
                    return self._json({"kind": "none"})
                if not key.lower().endswith(".pdf") or not RENDER.available:
                    return self._json({"kind": "other"})
                store = S["left_store"] if side == "left" else S["right_store"]
                sizes = RENDER.pages(f"{side}:{sid}", store.cached_read(key))
                return self._json({"kind": "pdf", "pages": sizes})
            except Exception as exc:  # noqa: BLE001 -- fall back to the plugin
                return self._json({"kind": "other", "why": str(exc)[:200]})
        if path.startswith("/page/"):
            try:
                _, _, side, sid, n = path.split("/", 4)
                row = S["rows"][int(sid)]
                key = row["left"] if side == "left" else row["right"]
                store = S["left_store"] if side == "left" else S["right_store"]
                png = RENDER.page_png(f"{side}:{sid}", store.cached_read(key),
                                      int(n.split(".")[0]))
                return self._send(png, "image/png")
            except Exception as exc:  # noqa: BLE001
                return self._send(str(exc).encode()[:300], "text/plain", 404)
        if path.startswith("/doc/"):
            try:
                _, _, side, sid = path.split("/", 3)
                row = S["rows"][int(sid)]
                key = row["left"] if side == "left" else row["right"]
                if key is None:
                    return self._send(b"no counterpart", "text/plain", 404)
                store = S["left_store"] if side == "left" else S["right_store"]
                data = store.cached_read(key)
                # Warm the next document while this one is on screen. On an
                # s3:// side each read is a round trip, and a reviewer moving
                # at one document every few seconds would feel every one.
                nxt = S["rows"][int(sid) + 1] if int(sid) + 1 < len(S["rows"]) else None
                if nxt:
                    nkey = nxt["left"] if side == "left" else nxt["right"]
                    if nkey:
                        store.prefetch(nkey)
            except Exception as exc:  # noqa: BLE001
                return self._send(str(exc).encode()[:400], "text/plain", 404)
            name = Path(key).name
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            # HTTP headers are latin-1. A macOS screenshot is named with a
            # narrow no-break space (U+202F) and crashed send_header mid
            # response, so the browser got an empty reply and the pane went
            # blank with no error anywhere the reviewer could see. Send an
            # ASCII-safe name plus the real one per RFC 5987.
            ascii_name = name.encode("ascii", "replace").decode("ascii").replace('"', "'")
            quoted = urllib.parse.quote(name, safe="")
            return self._send(data, ctype, 200, extra=[(
                "Content-Disposition",
                f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}",
            )])
        self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if path == "/api/browse":
            return self._json(native_picker())
        if path == "/api/inspect":
            try:
                return self._json(inspect_root(
                    (body.get("root") or "").strip(),
                    (body.get("profile") or "").strip() or None))
            except Exception as exc:  # noqa: BLE001 -- surfaced in the popup
                return self._json({"error": f"{type(exc).__name__}: {exc}"})
        if path == "/api/open":
            try:
                return self._json(open_review(
                    root=(body.get("root") or "").strip() or None,
                    left=(body.get("left") or "").strip() or None,
                    right=(body.get("right") or "").strip() or None,
                    profile=(body.get("profile") or "").strip() or None,
                    source=(body.get("source") or "").strip() or None,
                    output=(body.get("output") or "").strip() or None,
                ))
            except Exception as exc:  # noqa: BLE001 -- surfaced in the popup
                return self._json({"error": f"{type(exc).__name__}: {exc}"})
        if path == "/api/mark":
            if not S.get("ready"):
                return self._json({"error": "no session"}, 409)
            k, r = body.get("key"), body.get("rec") or {}
            rec = {"viewed": bool(r.get("viewed")),
                   "reviewed": bool(r.get("reviewed")),
                   "comments": [c for c in (r.get("comments") or []) if str(c).strip()]}
            with _LOCK:
                if rec["viewed"] or rec["reviewed"] or rec["comments"]:
                    S["marks"][k] = rec
                else:
                    S["marks"].pop(k, None)
                _write_json(MARKS, S["all_marks"])
            return self._json({})
        self._json({"error": "not found"}, 404)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", nargs="?", help="folder, .zip or s3:// prefix holding both halves")
    ap.add_argument("--left", help="source, when the two halves live apart")
    ap.add_argument("--right", help="output, when the two halves live apart")
    ap.add_argument("--source", help="folder name of the source half inside root")
    ap.add_argument("--output", help="folder name of the output half inside root")
    ap.add_argument("--profile", help="AWS profile for s3:// locations")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    if a.root and not (a.source or a.output):
        guess = inspect_root(a.root, a.profile)
        if guess.get("error"):
            print("  " + guess["error"], flush=True)
            return
        a.source, a.output = guess["source"], guess["output"]
        opts = ", ".join(f"{o['name']}/ ({o['files']})" for o in guess["options"])
        print(f"  inside: {opts}", flush=True)
        print(f"  using source={a.source or 'everything else'}  output={a.output}", flush=True)
        if guess.get("warn"):
            print(f"  note: {guess['warn']}", flush=True)
    if a.root or (a.left and a.right):
        open_review(root=a.root, left=a.left, right=a.right, profile=a.profile,
                    source=a.source, output=a.output)

    url = f"http://127.0.0.1:{a.port}/"
    print(f"\n  {url}\n", flush=True)
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    with Server(("127.0.0.1", a.port), Handler) as srv:
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped. verdicts are in", MARKS, flush=True)


if __name__ == "__main__":
    main()
