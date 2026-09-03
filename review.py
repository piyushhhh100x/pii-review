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
import io
import json
import mimetypes
import random
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
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


# --- rendering a document the browser would otherwise DOWNLOAD ---------------
# A browser saves ``text/csv`` and every spreadsheet type instead of showing
# them, so an iframe pointed at one pops a download dialog and leaves the pane
# blank -- on every refresh, twice, once per side. Rendering them here also
# means they are ordinary DOM, so the two panes scroll together like PDFs do.
_MAX_ROWS = 3000
_TABLE_EXTS = {".csv", ".tsv"}
_SHEET_EXTS = {".xlsx", ".xlsm"}
_WORD_EXTS = {".docx", ".docm"}
_SLIDE_EXTS = {".pptx", ".pptm"}
_JSON_EXTS = {".json", ".geojson", ".ipynb"}
_JSONL_EXTS = {".jsonl", ".ndjson"}
_XML_EXTS = {".xml", ".rss", ".atom", ".svg.xml"}
_TEXT_EXTS = {".txt", ".md", ".html", ".htm",
              ".eml", ".log", ".yaml", ".yml", ".ini", ".cfg",
              ".sql", ".vcf", ".ics", ".env"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}


#: Below this a column is too narrow to read, so the table is given a
#: min-width and the pane scrolls sideways instead of crushing every column.
#: A 40-column takeout sheet at table-layout:fixed gave each one about 15px.
_MIN_COL_PX = 150


def _table_html(rows) -> str:
    import html as _html
    head = rows[0] if rows else []
    cols = max((len(r) for r in rows), default=0)
    # Only widen when it is actually needed. A three-column sheet should fill
    # the pane, not sit in a 450px strip with the rest of the pane blank.
    style = f' style="min-width:{cols * _MIN_COL_PX}px"' if cols > 6 else ""
    out = [f"<table{style}><thead><tr><th class=n></th>"]
    out += [f"<th>{_html.escape(str(c))}</th>" for c in head]
    out.append("</tr></thead><tbody>")
    for n, row in enumerate(rows[1:_MAX_ROWS], 1):
        out.append(f"<tr><td class=n>{n}</td>")
        out += [f"<td>{_html.escape(str(c))}</td>" for c in row]
        out.append("</tr>")
    out.append("</tbody></table>")
    if len(rows) - 1 > _MAX_ROWS:
        out.append(f"<p class=more>{len(rows) - 1 - _MAX_ROWS} more rows not shown</p>")
    return "".join(out)


#: Longest cell the table renderer will print in full. A Discord or Google
#: takeout row carries an entire JSON blob in one field -- 302 KB in one cell
#: in the demo corpus -- and pasting that into a <td> makes a pane no reviewer
#: can scroll. The tail is dropped for display only; nothing here is written
#: back, and the metrics panel still reads the untouched bytes.
_MAX_CELL = 4000


# --- structured text, formatted so the two panes line up --------------------
# The pipeline reserialises what it rewrites. A source line reads
# ``{"self":"...","id":"10022"`` and its own output reads
# ``{"self": "https://...", "id": "10022"`` -- same data, different spacing,
# so the panes wrapped differently and scroll sync compared unrelated lines.
# Formatting BOTH sides with one formatter is what makes them comparable; the
# highlighting is the part that makes a wall of JSON readable at all.

_JSON_MAX_CHARS = 900_000
#: Records of a .jsonl shown in full. Past this the file is a shard dump, and
#: the reviewer wants the shape, not all of it.
_JSONL_MAX_RECORDS = 400


def _jhtml(obj, indent: int = 0, out=None) -> list:
    """Pretty JSON as highlighted HTML, walked rather than regexed.

    Emitting from the parsed object means the classes cannot land on the wrong
    span: a colon inside a string value is a colon inside a string value, not
    a key separator, which is exactly where a regex highlighter gives up on
    the ``"content":"https://...?a=b:c"`` URLs all over this corpus.
    """
    import html as _html
    out = [] if out is None else out
    pad, pad2 = "  " * indent, "  " * (indent + 1)
    if isinstance(obj, dict):
        if not obj:
            out.append('<span class=jp>{}</span>')
            return out
        out.append('<span class=jp>{</span>\n')
        for n, (k, v) in enumerate(obj.items()):
            out.append(pad2)
            out.append(f'<span class=jk>"{_html.escape(str(k))}"</span>'
                       '<span class=jp>: </span>')
            _jhtml(v, indent + 1, out)
            out.append('<span class=jp>,</span>\n' if n < len(obj) - 1 else "\n")
        out.append(pad + '<span class=jp>}</span>')
    elif isinstance(obj, list):
        if not obj:
            out.append('<span class=jp>[]</span>')
            return out
        out.append('<span class=jp>[</span>\n')
        for n, v in enumerate(obj):
            out.append(pad2)
            _jhtml(v, indent + 1, out)
            out.append('<span class=jp>,</span>\n' if n < len(obj) - 1 else "\n")
        out.append(pad + '<span class=jp>]</span>')
    elif isinstance(obj, str):
        out.append(f'<span class=js>"{_html.escape(obj)}"</span>')
    elif isinstance(obj, bool) or obj is None:
        out.append(f'<span class=jb>{"null" if obj is None else str(obj).lower()}</span>')
    else:
        out.append(f'<span class=jn>{_html.escape(str(obj))}</span>')
    return out


def _json_html(data: bytes) -> str:
    import json as _json
    text = data.decode("utf8", "replace")
    if len(text) > _JSON_MAX_CHARS:
        raise ValueError("too large to format")
    return "<pre>" + "".join(_jhtml(_json.loads(text))) + "</pre>"


def _jsonl_html(data: bytes) -> str:
    """One numbered, formatted record per row.

    Numbered because a comment on a shard is always "record 12", and because
    the number is the anchor that keeps the eye on the same record in both
    panes when the rewritten side is a different length.
    """
    import html as _html
    import json as _json
    lines = data.decode("utf8", "replace").splitlines()
    rows, n = [], 0
    for raw in lines:
        if not raw.strip():
            continue
        n += 1
        if n > _JSONL_MAX_RECORDS:
            break
        try:
            body = "".join(_jhtml(_json.loads(raw)))
        except Exception:  # noqa: BLE001 -- a bad line is itself worth seeing
            body = f'<span class=jbad>{_html.escape(raw)}</span>'
        rows.append(f'<div class=rec><div class=recn>{n}</div>'
                    f'<pre class=recb>{body}</pre></div>')
    kept = sum(1 for r in lines if r.strip())
    if kept > _JSONL_MAX_RECORDS:
        rows.append(f'<p class=more>{kept - _JSONL_MAX_RECORDS} more records '
                    f'not shown</p>')
    return "".join(rows)


def _xml_html(data: bytes) -> str:
    """Indented XML. Same bargain as the JSON: both sides get one shape."""
    import html as _html
    import xml.dom.minidom as _md
    text = data.decode("utf8", "replace")
    if len(text) > _JSON_MAX_CHARS:
        raise ValueError("too large to format")
    pretty = _md.parseString(text).toprettyxml(indent="  ")
    # minidom leaves a blank line wherever the input already had whitespace.
    pretty = "\n".join(l for l in pretty.splitlines() if l.strip())
    return f"<pre>{_html.escape(pretty)}</pre>"


def _rows_from_csv(data: bytes):
    import csv, io as _io
    text = data.decode("utf8", errors="replace")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except Exception:  # noqa: BLE001
        dialect = csv.excel
    # csv defaults to a 128 KB field cap and raises past it. That exception was
    # the download bug: the table route failed, view() fell through to the raw
    # byte route, and the browser saved users.csv to disk instead of showing
    # it. Lift the cap for this parse only -- it is process-global state, so it
    # is restored even when the read raises.
    was = csv.field_size_limit()
    try:
        csv.field_size_limit(max(was, len(text) + 1))
        rows = list(csv.reader(_io.StringIO(text), dialect))
    finally:
        csv.field_size_limit(was)
    return [[c if len(c) <= _MAX_CELL else c[:_MAX_CELL] + " …" for c in r]
            for r in rows]


def _col_index(ref: str) -> int:
    """``C7`` -> 2. Sheet XML omits empty cells, so a row has to be placed by
    its column letters or every value after a gap shifts left."""
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + (ord(ch.upper()) - 64)
    return max(0, n - 1)


_DOCX_CHROME = re.compile(r"word/(?:header|footer)\d*\.xml$")


def _text_from_docx(data: bytes) -> str:
    """Paragraph and table text of a .docx, using only the standard library.

    Same bargain as _rows_from_xlsx: a docx is a zip of XML, so the no-
    dependency rule costs nothing. Without this every Word document fell
    through to the raw byte route and the browser SAVED it instead of showing
    it -- which in a redaction review means the one format most likely to
    carry an offer letter or a contract could not be eyeballed at all.

    Paragraph splits are what matter here. A run boundary lands mid-sentence
    wherever the author changed formatting, and the rewriter frequently
    replaces a name that spans two runs, so joining runs without a separator
    is the only way the two panes stay comparable line for line.
    """
    import zipfile
    import xml.etree.ElementTree as ET

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        have = set(z.namelist())
        body = next((n for n in ("word/document.xml", "word/document2.xml")
                     if n in have), None)
        if body is None:
            raise ValueError("no word/document.xml")
        # Headers and footers carry running contact blocks -- the offer letter
        # in the eval corpus keeps its author's email only in footer1. Reading
        # the body alone would show that address as absent from BOTH panes,
        # which reads as "clean" when it is really "not looked at".
        chrome = sorted(n for n in have
                        if _DOCX_CHROME.match(n))
        parts = [z.read(n) for n in [body, *chrome]]

    roots = []
    for raw in parts:
        try:
            roots.append(ET.fromstring(raw))
        except ET.ParseError:
            continue

    out: list[str] = []
    for para in (p for r in roots for p in r.iter(f"{W}p")):
        buf: list[str] = []
        for node in para.iter():
            tag = node.tag
            if tag == f"{W}t":
                buf.append(node.text or "")
            elif tag in (f"{W}tab",):
                buf.append("\t")
            elif tag in (f"{W}br", f"{W}cr"):
                buf.append("\n")
        line = "".join(buf)
        if line.strip() or (out and out[-1].strip()):
            out.append(line)
        if len(out) > _MAX_ROWS:
            out.append("... more paragraphs not shown")
            break
    return "\n".join(out).strip()


def _text_from_pptx(data: bytes) -> str:
    """Slide text of a .pptx, in slide order, standard library only.

    Decks reach a redaction review as customer-facing collateral -- the QBR
    with the account contacts on slide 2 -- and every one of them used to end
    at the raw byte route, which is to say at a download. Slides are numbered
    rather than sorted as strings so slide10 does not land between slide1 and
    slide2, and the number is printed because "which slide" is the first thing
    a reviewer writes in a comment.
    """
    import zipfile
    import xml.etree.ElementTree as ET

    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist()
                 if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
        if not names:
            raise ValueError("no ppt/slides")
        names.sort(key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
        parts = [(n, z.read(n)) for n in names]

    out: list[str] = []
    for name, raw in parts:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        n = re.search(r"(\d+)", name.rsplit("/", 1)[1]).group(1)
        out.append(f"--- slide {n} ---")
        for para in root.iter(f"{A}p"):
            line = "".join(t.text or "" for t in para.iter(f"{A}t"))
            out.append(line)
        out.append("")
        if len(out) > _MAX_ROWS:
            out.append("... more slides not shown")
            break
    return "\n".join(out).strip()


def _rows_from_xlsx(data: bytes):
    """First sheet of an xlsx, using only the standard library.

    This tool has no third-party dependencies on purpose -- it is a stdlib
    http.server and shells out to a separate interpreter for PDF rendering --
    and an xlsx is a zip of XML, so reading one needs no exception to that.
    Without this the 138 spreadsheets in a finance batch fell through to the
    raw byte route, and the browser SAVED each one instead of showing it.
    """
    import zipfile
    import xml.etree.ElementTree as ET

    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(f"{NS}t"))
                      for si in root.findall(f"{NS}si")]
        sheets = sorted(n for n in z.namelist()
                        if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        if not sheets:
            return []
        rows: list[list[str]] = []
        for row in ET.fromstring(z.read(sheets[0])).iter(f"{NS}row"):
            cells: list[str] = []
            for c in row.findall(f"{NS}c"):
                at = _col_index(c.get("r", ""))
                while len(cells) < at:
                    cells.append("")
                v = c.find(f"{NS}v")
                if c.get("t") == "s" and v is not None and v.text is not None:
                    cells.append(shared[int(v.text)] if int(v.text) < len(shared) else "")
                elif c.get("t") == "inlineStr":
                    cells.append("".join(t.text or "" for t in c.iter(f"{NS}t")))
                else:
                    cells.append(v.text if v is not None and v.text else "")
            rows.append(cells)
            if len(rows) > _MAX_ROWS:
                break
        width = max((len(r) for r in rows), default=0)
        for r in rows:
            r.extend([""] * (width - len(r)))
        return rows


def _looks_textual(data: bytes) -> str | None:
    """The decoded text if these bytes are readable, else None.

    The extension lists will always trail the corpus -- every batch turns up
    some ``.ndjson`` or ``.properties`` nobody listed. Sniffing catches those
    without a new release, which matters because the alternative was not "a
    plainer view", it was a save dialog.
    """
    head = data[:8192]
    if b"\x00" in head:
        return None
    try:
        text = data.decode("utf8")
    except UnicodeDecodeError:
        return None
    # Control characters other than tab/newline/return mean binary that merely
    # happens to decode.
    if sum(c < 32 and c not in (9, 10, 13) for c in head) > len(head) // 100:
        return None
    return text


def view(key: str, data: bytes) -> dict:
    """How this document should be shown, and the payload to show it with.

    Never returns a shape the browser would download. Every branch ends at
    something the page draws itself, at the PDF plugin, or at an explicit
    "cannot show this" card -- because an iframe pointed at a document Chrome
    will not render does not fail visibly, it silently saves the file and
    leaves the pane blank.
    """
    import html as _html
    ext = Path(key).suffix.lower()
    if ext == ".pdf":
        return {"kind": "pdf"}
    try:
        if ext in _TABLE_EXTS:
            return {"kind": "table", "html": _table_html(_rows_from_csv(data))}
        if ext in _SHEET_EXTS:
            return {"kind": "table", "html": _table_html(_rows_from_xlsx(data))}
        if ext in _WORD_EXTS:
            return {"kind": "text",
                    "html": f"<pre>{_html.escape(_text_from_docx(data))}</pre>"}
        if ext in _SLIDE_EXTS:
            return {"kind": "text",
                    "html": f"<pre>{_html.escape(_text_from_pptx(data))}</pre>"}
        # Formatting is best-effort on purpose. A file that does not parse is
        # still a file the reviewer has to look at -- and "the rewriter emitted
        # broken JSON" is itself the finding -- so a failure here drops to the
        # raw text below rather than to a card saying it could not be shown.
        if ext in _JSONL_EXTS:
            try:
                return {"kind": "records", "html": _jsonl_html(data)}
            except Exception:  # noqa: BLE001
                pass
        if ext in _JSON_EXTS:
            try:
                return {"kind": "text", "html": _json_html(data)}
            except Exception:  # noqa: BLE001
                pass
        if ext in _XML_EXTS:
            try:
                return {"kind": "text", "html": _xml_html(data)}
            except Exception:  # noqa: BLE001
                pass
        if ext in _JSON_EXTS | _JSONL_EXTS | _XML_EXTS:
            return {"kind": "text",
                    "html": f"<pre>{_html.escape(data.decode('utf8', 'replace'))}</pre>"}
        if ext in _TEXT_EXTS:
            return {"kind": "text",
                    "html": f"<pre>{_html.escape(data.decode('utf8', 'replace'))}</pre>"}
        if ext in _IMAGE_EXTS:
            return {"kind": "image"}
        sniffed = _looks_textual(data)
        if sniffed is not None:
            return {"kind": "text", "html": f"<pre>{_html.escape(sniffed)}</pre>"}
    except Exception as exc:  # noqa: BLE001 -- shown on the card, not swallowed
        return {"kind": "other", "ext": ext,
                "why": f"{type(exc).__name__}: {exc}"[:200]}
    return {"kind": "other", "ext": ext,
            "why": _CANNOT.get(ext, "no viewer for this file type")}


#: Formats with no standard-library reader. Saying so on the card beats the
#: old behaviour, which was to hand the bytes to the browser and hope.
_CANNOT = {
    ".doc": "legacy binary Word — re-export as .docx to review it here",
    ".xls": "legacy binary Excel — re-export as .xlsx to review it here",
    ".ppt": "legacy binary PowerPoint — re-export as .pptx to review it here",
}


def session_id(left: str, right: str) -> str:
    """Stable id for a (left, right) pair, so verdicts survive a re-open."""
    h = hashlib.sha256(f"{left}\x00{right}".encode()).hexdigest()[:12]
    return f"{Path(right.rstrip('/')).name or right}-{h}"


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Review</title><style>
.doc{background:#fff;padding:10px 12px}
/* A wide sheet scrolls sideways rather than squeezing 40 columns into the
   pane. Both panes do it independently -- the sync mirrors vertical position,
   which is the axis a reviewer moves down. */
.doc.table{overflow-x:auto}
/* Fixed layout, not auto. One takeout row carries a multi-kilobyte JSON blob,
   and auto layout hands that column the whole pane -- every other heading
   collapses to one letter per line and the two sides stop lining up. Fixed
   gives each column an equal share and lets the long one wrap. */
.doc table{border-collapse:collapse;font-size:12.5px;width:100%;table-layout:fixed}
/* break-word, not anywhere. "anywhere" split "Galih Eka Putra" across two
   lines as "G / alih Eka Putra" -- and a person's name is the single thing a
   reviewer is scanning these cells for. break-word keeps words whole and
   still breaks the base64 blobs that have no break opportunity at all. */
.doc th,.doc td{border:1px solid #e3e6ea;padding:3px 6px;text-align:left;
  vertical-align:top;white-space:pre-wrap;word-break:normal;overflow-wrap:break-word}
.doc thead th{position:sticky;top:0;background:#f5f6f8;font-weight:600;z-index:1;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.doc th.n,.doc td.n{color:#999;text-align:right;font-variant-numeric:tabular-nums;
  background:#fafbfc;width:42px;white-space:nowrap}
.doc pre{margin:0;font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
  white-space:pre-wrap;word-break:normal;overflow-wrap:break-word}
.doc .more{color:#888;font-size:12px;padding:6px 2px}

/* Formatted JSON. Keys carry the colour because a reviewer scanning for a
   leak is scanning field names first -- emailAddress, displayName, phone --
   and only then reading the value beside it. */
.doc .jk{color:#1a4f8a}
.doc .js{color:#0b6b5e}
.doc .jn{color:#a15c00}
.doc .jb{color:#7a3ea1}
.doc .jp{color:#9aa0a6}
.doc .jbad{color:var(--bad)}

/* One record of a .jsonl. The number is a gutter, not content: it stays put
   while the record beside it wraps, so the same record sits at the same mark
   in both panes even when the rewritten side is a different length. */
.doc.records{padding:0}
.rec{display:flex;gap:0;border-bottom:1px solid #eef0f2;align-items:stretch}
.rec:last-child{border-bottom:0}
.recn{flex:0 0 44px;padding:8px 8px 8px 0;text-align:right;color:#b0b5ba;
  background:#fafbfc;border-right:1px solid #eef0f2;font:11.5px/1.5 ui-monospace,
  SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums;
  position:sticky;left:0}
.recb{flex:1;min-width:0;padding:8px 10px}
.opt{color:var(--mut);font-weight:400;font-size:11px}
.loc{font-weight:600;font-size:13px;padding:2px 8px;border-radius:5px;background:#eef1f5;
     color:#333;white-space:nowrap;max-width:22ch;overflow:hidden;text-overflow:ellipsis}
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
.empty .why{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;opacity:.8}
.empty .raw{display:inline-block;margin-top:10px;color:var(--fg);text-decoration:underline}

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
  <div class="row"><label for="label">Name <span class="opt">optional</span></label>
    <div class="grow"><input type="text" id="label" spellcheck="false"
      placeholder="finance 28th — defaults to the folder name"></div></div>
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
  <span class="loc" id="loc" title=""></span>
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
// The profile box appears for anything that names S3, not just a typed
// "s3://". Keyed on that literal alone, a pasted console URL or an ARN left
// the field hidden -- so the one credential the request needed could not be
// entered, and Open came back with an unhelpful access error.
// Note the two different hosts: the REST endpoints are *.amazonaws.com, but
// the console is console.aws.amazon.com -- which does not contain the string
// "amazonaws.com" at all. Matching only the first spelling left the field
// hidden for exactly the URL people actually paste.
const NAMES_S3=/s3:\/\/|amazonaws\.com|aws\.amazon\.com\/s3|arn:aws[a-z-]*:s3:/i;
function syncProf(){el("profrow").style.display=
  NAMES_S3.test(el("root").value+" "+el("left").value+" "+el("right").value)?"":"none";}
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
  // Show what was actually opened. A pasted console URL becomes the s3:// URI
  // here, which is both the confirmation that the paste was understood and
  // the string the recents list and marks.json will be keyed by.
  let msg=r.warn||"Found both halves. Change either if this is not right.";
  if(r.root&&r.root!==root){ el("root").value=r.root;
    msg=r.warn||("Opening "+r.root+" — change either half if this is not right."); }
  note(msg,r.warn?"warn":"");
}
el("root").addEventListener("keydown",e=>{if(e.key==="Enter"){inspected?open_():inspect();}});
async function open_(){
  el("go").disabled=true;note("Reading both sides…");
  const lbl=el("label")?el("label").value.trim():"";
  const body=twoWay?{left:el("left").value,right:el("right").value,profile:el("profile").value,label:lbl}
                   :{root:el("root").value,profile:el("profile").value,source:el("src").value,output:el("out").value,label:lbl};
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
  // Two tabs on two batches look identical without this.
  const where=(r.label||"").trim()
    || (r.root||"").replace(/\/+$/,"").split("/").pop() || r.root || "review";
  el("loc").textContent=where; el("loc").title=r.root||"";
  document.title=where+" · "+(r.pairs?r.pairs.length:0)+" docs";
  if(r.hint){el("btext").textContent=r.hint.text;el("bfix").textContent="use "+r.hint.output+"/";
    el("bfix").onclick=()=>{el("out").value=r.hint.output;el("banner").classList.remove("on");open_();};
    el("bfix").style.display=""; el("banner").classList.add("on");}
  else if(r.scope_note){
    // Not a warning and not something to fix -- a fact about what the chosen
    // location covers. It carries no button, because there is nothing here
    // the reviewer got wrong.
    el("btext").textContent=r.scope_note; el("bfix").style.display="none";
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
  // Rendered here rather than handed to the browser: it SAVES a csv/xlsx
  // instead of showing one. Rendering also makes them ordinary DOM, so these
  // panes scroll in step exactly like the PDF ones.
  if(meta.kind==="table"||meta.kind==="text"||meta.kind==="records"){
    const sc=document.createElement("div");
    sc.className="scroll doc "+meta.kind;
    sc.innerHTML=meta.html||"";
    box.appendChild(sc); return sc;
  }
  if(meta.kind==="image"){
    const sc=document.createElement("div"); sc.className="scroll";
    const img=new Image(); img.src="/doc/"+side+"/"+id; img.style.width="100%";
    sc.appendChild(img); box.appendChild(sc); return sc;
  }
  if(meta.kind==="other"){
    // Never an iframe. Chrome does not "fail to render" a .doc or a broken
    // xlsx -- it saves it, silently, once per pane per refresh, and leaves
    // the reviewer looking at a blank box wondering why Downloads is full.
    // Say what it is, and make the download something they choose.
    const why=meta.why?("<br><span class=why>"+esc(meta.why)+"</span>"):"";
    box.innerHTML="<div class='empty'><b>Can't show this one here.</b>"+why+
      "<br><a class='raw' href='/doc/"+side+"/"+id+"' download>Download the raw file</a></div>";
    return null;
  }
  if(meta.kind==="raw"){box.appendChild(iframeFor(side,id));return null;}
  if(meta.kind!=="pdf"||!meta.pages){
    // "none", or any shape a future server sends that this page predates.
    // The old catch-all was the iframe, i.e. a download; this one is a
    // sentence.
    box.innerHTML="<div class='empty'>Nothing to show for this side.</div>";
    return null;
  }
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
  let lm={kind:"other",why:"could not ask the server about this document"},rm={kind:"none"};
  if(CAN){ try{
    lm=await (await fetch("/api/doc/left/"+p.id)).json();
    if(p.right) rm=await (await fetch("/api/doc/right/"+p.id)).json();
  }catch(e){} }
  if(seq!==SEQ) return;                 // a slower answer for a document already left behind
  LS=build_pane(el("lp"),"left",p.id,lm);
  if(p.right) RS=build_pane(el("rp"),"right",p.id,rm);
  else el("rp").innerHTML="<div class='empty'><b>Nothing in the output for this document.</b><br>"+
        "Either the run withheld it, or it was never processed.</div>";
  if(lm.kind==="records"&&rm.kind==="records") alignRecords(LS,RS);
  linkScroll(LS,RS);
}

/* Record N starts at the same height on both sides.
   Proportional scroll sync is the right answer for a PDF, where the two sides
   are the same length. It is the wrong one here: the rewriter replaces a long
   gravatar URL with "https://example.com", so the source record is taller,
   and by record 20 the panes are showing different records. Pad the shorter
   of each pair instead and the numbers stay level all the way down. */
function alignRecords(a,b){
  if(!a||!b)return;
  const la=[...a.querySelectorAll(".rec")], lb=[...b.querySelectorAll(".rec")];
  const n=Math.min(la.length,lb.length);
  // Clear first, or a re-align after a resize measures the previous padding
  // and every record grows a little taller each time.
  for(const r of la.concat(lb)) r.style.minHeight="";
  requestAnimationFrame(()=>{
    const h=[];
    for(let i=0;i<n;i++) h.push(Math.max(la[i].offsetHeight,lb[i].offsetHeight));
    for(let i=0;i<n;i++){ la[i].style.minHeight=h[i]+"px"; lb[i].style.minHeight=h[i]+"px"; }
  });
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
        # A location pasted from the console usually points at the output
        # half. Both halves are one level up.
        st = stores.open_store(resolve_root(root), profile)
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


def resolve_root(spec: str) -> str:
    """Canonical location for a single-location request.

    Order matters and cost a debugging round: a console URL carries the run's
    depth in ``?prefix=``, not in its path, so climbing out of the output half
    has to happen AFTER the URL is turned into an s3:// URI. Climbing first
    silently did nothing, and the review opened on the output half alone.
    """
    s3 = stores.parse_s3(spec)
    return pairing.run_root(s3[0] if s3 else (spec or "").strip())


def inspect_root(root: str, profile: str | None) -> dict:
    """What the two halves inside one location look like."""
    root = resolve_root(root)
    st = stores.open_store(root, profile)
    # Deliberately the RAW listing, not ``docs``: an in-place run puts its
    # output under ``_pii/output``, which the document filter drops. It has to
    # be visible here or it could never be chosen as a side.
    paths = [p for p in st._list() if "__MACOSX" not in p]
    if not paths:
        return {"error": f"nothing found in {root}"}
    out = pairing.autosplit(paths)
    # Hand back what was actually opened. The popup writes this into the
    # location box, so a pasted console URL visibly becomes the s3:// URI the
    # recents list and marks.json will be keyed by -- one run, one entry.
    out["root"] = st.spec
    return out


#: Pairs kept per app folder. Nobody reviews an export end to end -- the job
#: is a few files per app and a search for the handful somebody reported -- and
#: a list of 121,538 is one nobody can navigate.
PER_APP = 100


def _thin(rows, per_app: int):
    """At most ``per_app`` pairs per app folder, chosen at random.

    Per app, not per run: a single budget spent top-down would go entirely to
    whichever app sorts first and show none of the rest.

    Random rather than the first N, because S3 hands back keys in sort order,
    so the head of the list is always page_000001 of whoever sorts first --
    the same corner of the same export every time, with whole categories of
    defect never on screen.

    Seeded on the app name, so the same run thins to the same files every time
    it is opened. Verdicts in marks.json are keyed by path, and a sample that
    reshuffled on reopen would strand yesterday's review against files nobody
    can see today.
    """
    if not per_app:
        return rows, {}
    by_app: dict[str, list] = {}
    for r in rows:
        by_app.setdefault(r["label"].split("/")[0], []).append(r)
    kept, dropped = [], {}
    for app, group in by_app.items():
        if len(group) <= per_app:
            kept += group
            continue
        dropped[app] = len(group) - per_app
        kept += random.Random(app).sample(group, per_app)
    kept.sort(key=lambda r: r["label"])
    return kept, dropped


def open_review(root=None, left=None, right=None, profile=None,
                source=None, output=None, ignore=None, label=None,
                per_app: int = PER_APP) -> dict:
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
    rows, thinned = _thin(rows, per_app)
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
        # ``ls.spec`` not ``root``: the store holds the canonical s3:// form,
        # so a console URL and the URI for the same run make one recents entry
        # instead of two.
        spec = ls.spec if root else left
        recent = _read_json(RECENT, [])
        recent = [spec] + [x for x in recent if x != spec]
        _write_json(RECENT, recent[:10])

    counts = dict(idx["counts"])
    counts["by_how"] = dict(counts["by_how"])
    counts["by_how"]["missing"] = len(idx["unmatched_left"])
    print(f"  {len(rows)} documents  {counts['by_how']}", flush=True)
    if idx["unmatched_right"]:
        print(f"  {len(idx['unmatched_right'])} output file(s) with no source", flush=True)

    # Reported, never silent. A bucket can hold several runs' worth of export,
    # and the documents belonging to the OTHER ones are not findings -- but
    # "your location covers more than this run" is worth a line, and hiding
    # rows without saying so would be worse than the noise it removes.
    if thinned:
        named = ", ".join(f"{a}/ ({n} more)" for a, n in
                          sorted(thinned.items(), key=lambda t: -t[1])[:4])
        print(f"  showing {per_app} per app — not shown: {named}", flush=True)

    units = idx.get("out_of_scope_units") or {}
    scope_note = None
    if units:
        ranked = sorted(units.items(), key=lambda t: -t[1])
        total = sum(units.values())
        # One branch is the common case and reads better without its own
        # count repeated back at you; several need the breakdown.
        named = ranked[0][0] + "/" if len(ranked) == 1 else \
            ", ".join(f"{u}/ ({n})" for u, n in ranked[:3])
        more = "" if len(ranked) <= 3 else f" and {len(ranked) - 3} more"
        scope_note = (f"{total:,} document(s) under {named}{more} have no output at "
                      f"all, so this run did not cover them and they are not "
                      f"listed. Point at that location directly to review it.")
        print(f"  {scope_note}", flush=True)

    # Safety net. If almost nothing paired, the split is almost certainly
    # wrong — and the reviewer has no way to tell that from a screen of
    # "nothing in the output", which looks exactly like a pipeline that
    # dropped everything. So check whether another folder would do better and
    # offer it, rather than letting a wrong choice read as a catastrophic run.
    hint = None
    missing = len(idx["unmatched_left"])
    if root and rows and missing / len(rows) > 0.5:
        guess = pairing.autosplit([p for p in ls.paths if "__MACOSX" not in p])
        # Never offer a folder that the SOURCE is made of. On the run this was
        # written against the banner said "jira/ holds far more — that is
        # probably the output folder", and jira/ was the source: taking the
        # advice would have compared the run against itself. A folder every
        # source path already sits under cannot be the other half.
        srcseg = {seg.lower() for p in ls.docs for seg in pairing.segments(p)[:-1]}
        outseg = {seg.lower() for p in rs.docs for seg in pairing.segments(p)[:-1]}
        better = [o for o in guess["options"]
                  if o["name"] not in (source, output) and o["files"] > missing * 0.5
                  and not (o["name"].lower() in srcseg and o["name"].lower() not in outseg)]
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
               "scope_note": scope_note,
               "thinned": thinned,
               "left_short": short(ls, source), "right_short": short(rs, output),
               "source": source, "output": output, "root": root,
               # What the browser tab is named. Two tabs on two batches are
               # otherwise identical, which is how a reviewer ends up reading
               # the wrong day's output.
               "label": (label or "").strip()}
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
                store = S["left_store"] if side == "left" else S["right_store"]
                data = store.cached_read(key)
                shape = view(key, data)
                if shape["kind"] == "pdf":
                    if not RENDER.available:
                        # The one type the browser really does display in a
                        # frame. Kept on the iframe route deliberately: without
                        # PyMuPDF the plugin viewer is the only way to read a
                        # PDF here, and it shows rather than saves.
                        return self._json({"kind": "raw"})
                    return self._json({"kind": "pdf",
                                       "pages": RENDER.pages(f"{side}:{sid}", data)})
                return self._json(shape)
            except Exception as exc:  # noqa: BLE001 -- surfaced on the card
                return self._json({"kind": "other",
                                   "why": f"{type(exc).__name__}: {exc}"[:200]})
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
            # Anything textual is served as text/plain. A browser SAVES
            # text/csv, which is the download dialog this route used to pop.
            if ctype.startswith("text/") and not ctype.startswith("text/html"):
                ctype = "text/plain; charset=utf-8"
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
                    label=(body.get("label") or "").strip() or None,
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


def _name_for(spec: str) -> str:
    """A short, distinguishable tab name for a location."""
    return Path(str(spec).rstrip("/")).name or str(spec)


def plan(a) -> list[dict]:
    """Every review the arguments ask for, in the order they were given.

    One is the ordinary case and runs in this process; more than one fans out
    to a server each. Kept separate from ``main`` so the collecting can be
    tested without starting anything.
    """
    jobs = [{"root": r, "label": a.label or _name_for(r)} for r in (a.root or [])]
    jobs += [{"left": l, "right": r,
              "label": a.label or f"{_name_for(l)} → {_name_for(r)}"}
             for l, r in (a.pair or [])]
    if a.left and a.right:
        jobs.append({"left": a.left, "right": a.right,
                     "label": a.label or f"{_name_for(a.left)} → {_name_for(a.right)}"})
    return jobs


def _fan_out(jobs, a) -> int:
    """One review per location, each its own server, all opened at once.

    Comparing runs means having them side by side, and this tool holds one
    session per process -- the index, the marks and the render cache are all
    per-server state. Rather than teach every one of those to be
    multi-tenant, run one server per review and hand back the list of URLs.
    They are independent: marks.json is already keyed by the pair of
    locations, so three tabs cannot overwrite each other's verdicts.
    """
    import atexit
    import signal

    import tempfile

    kids, urls, logs = [], [], []
    for n, job in enumerate(jobs):
        port = a.port + n
        cmd = [sys.executable, str(Path(__file__).resolve()),
               "--port", str(port), "--no-open"]
        if job.get("root"):
            cmd.append(job["root"])
        else:
            cmd += ["--left", job["left"], "--right", job["right"]]
        for flag in ("profile", "source", "output"):
            if getattr(a, flag, None):
                cmd += [f"--{flag}", getattr(a, flag)]
        cmd += ["--label", job["label"]]
        # Each child's chatter goes to its own file rather than the shared
        # terminal. Three reviews indexing at once interleaved into an
        # unreadable braid, and the one thing a person needs from this command
        # is a clean list of URLs. A file, not a pipe: nobody is draining
        # these while they run, and a full pipe buffer would wedge the child.
        log = tempfile.NamedTemporaryFile(prefix="pii-review-", suffix=".log",
                                          delete=False)
        kids.append(subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT))
        logs.append(log.name)
        urls.append((job["label"], f"http://127.0.0.1:{port}/"))

    def stop(*_):
        for k in kids:
            if k.poll() is None:
                k.terminate()
        for f in logs:
            try:
                Path(f).unlink()
            except OSError:
                pass
    atexit.register(stop)
    signal.signal(signal.SIGTERM, lambda *_: (stop(), sys.exit(0)))

    # Indexing is a network walk per review, so they are told to be quiet and
    # the tabs open once each one actually answers. Opening first gave three
    # connection-refused pages, which reads as "the tool is broken".
    print(f"\n  starting {len(jobs)} reviews — this takes a moment each\n", flush=True)
    for (label, url), kid in zip(urls, kids):
        ready, waited = False, 0.0
        # Wait for as long as the child is alive rather than on a clock.
        # A fixed budget was wrong: indexing an S3 prefix is a paginated walk
        # of every key, which took minutes on the real runs, and a two-minute
        # cap reported "did not start" for reviews that were about to come up
        # perfectly. The child exiting is the only honest failure signal.
        while kid.poll() is None:
            try:
                urllib.request.urlopen(url, timeout=2).read(1)
                ready = True
                break
            except Exception:  # noqa: BLE001 -- still indexing
                time.sleep(1.0)
                waited += 1.0
                # Silence past half a minute reads as a hang, and the reason
                # it is slow (a large bucket) is worth naming.
                if waited % 30 == 0:
                    print(f"    …still indexing {label}  ({int(waited)}s)",
                          flush=True)
        if ready:
            print(f"    {url}   {label}", flush=True)
            if not a.no_open:
                webbrowser.open(url)
        else:
            # Silence is the wrong failure mode. Show why this one did not
            # come up, from its own log, instead of a bare "did not start".
            print(f"  ! {label} stopped before it was ready:", flush=True)
            why = Path(logs[urls.index((label, url))]).read_text()[-800:].strip()
            for line in (why or "it wrote nothing").splitlines()[-8:]:
                print(f"      {line}", flush=True)
    print("\n  Ctrl-C stops all of them.\n", flush=True)
    try:
        for k in kids:
            k.wait()
    except KeyboardInterrupt:
        stop()
        print("\nstopped. verdicts are in", MARKS, flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Side-by-side review of a redaction run.",
        epilog="Give several locations, or repeat --pair, to open several "
               "reviews at once — one browser tab each, on consecutive ports.")
    ap.add_argument("root", nargs="*",
                    help="folder, .zip or S3 location holding both halves. "
                         "Give more than one to open one review per location.")
    ap.add_argument("--pair", nargs=2, action="append", metavar=("SOURCE", "OUTPUT"),
                    help="a review whose two halves live apart. Repeatable.")
    ap.add_argument("--left", help="source, when the two halves live apart")
    ap.add_argument("--right", help="output, when the two halves live apart")
    ap.add_argument("--source", help="folder name of the source half inside root")
    ap.add_argument("--output", help="folder name of the output half inside root")
    ap.add_argument("--profile", help="AWS profile for S3 locations")
    ap.add_argument("--label", help="what to call this batch in the tab title")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()

    jobs = plan(a)
    if len(jobs) > 1:
        return _fan_out(jobs, a)

    a.root = a.root[0] if a.root else None
    if jobs and not a.root:
        a.left, a.right = jobs[0].get("left"), jobs[0].get("right")
    a.label = a.label or (jobs[0]["label"] if jobs else None)

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
                    label=a.label,
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
