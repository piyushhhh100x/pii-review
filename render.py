#!/usr/bin/env python3
"""Render PDF pages to PNG so both panes can be ordinary scrollable DOM.

The browser's built-in PDF viewer is a plugin document: its scroll position
cannot be read or written from the page around it, so two PDF iframes can never
be kept in step. Rendering to images removes the plugin from the picture and
makes scroll sync a one-line mirror of ``scrollTop``.

PyMuPDF does the rendering. It is usually not installed for the interpreter
running this tool, so a long-lived worker is started under whichever
interpreter *does* have it. One worker, kept warm, because paying ~250 ms of
interpreter startup per page felt like the jerk it was.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
from collections import OrderedDict
from pathlib import Path

WORKER = r'''
import json, struct, sys
import fitz

docs = {}
out = sys.stdout.buffer

def reply(obj, blob=b""):
    head = json.dumps(obj).encode()
    out.write(struct.pack("<II", len(head), len(blob)))
    out.write(head)
    out.write(blob)
    out.flush()

while True:
    raw = sys.stdin.buffer.read(8)
    if len(raw) < 8:
        break
    hlen, blen = struct.unpack("<II", raw)
    req = json.loads(sys.stdin.buffer.read(hlen))
    body = sys.stdin.buffer.read(blen)
    try:
        cmd = req["cmd"]
        if cmd == "open":
            d = fitz.open(stream=body, filetype="pdf")
            docs[req["key"]] = d
            reply({"ok": True, "pages": [[p.rect.width, p.rect.height] for p in d]})
        elif cmd == "page":
            d = docs[req["key"]]
            pm = d[req["n"]].get_pixmap(matrix=fitz.Matrix(req["zoom"], req["zoom"]))
            reply({"ok": True}, pm.tobytes("png"))
        elif cmd == "text":
            d = docs[req["key"]]
            reply({"ok": True}, "\n".join(p.get_text() for p in d).encode())
        elif cmd == "drop":
            docs.pop(req["key"], None)
            reply({"ok": True})
        else:
            reply({"ok": False, "error": "bad command"})
    except Exception as exc:
        reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
'''


CONF = Path(__file__).resolve().parent / ".renderer"


def _interpreters():
    """Interpreters worth probing for PyMuPDF, best guess first.

    Deliberately a short, targeted list. A recursive glob of the home
    directory found one in 52 seconds, which is 52 seconds of the tool looking
    broken on every start.
    """
    home = Path.home()
    cands = [
        os.environ.get("PII_REVIEW_PYTHON"),
        sys.executable,
        *(str(p) for p in sorted(home.glob("*/*/.venv/bin/python"))),
        *(str(p) for p in sorted(home.glob("*/*/*/.venv/bin/python"))),
        *(str(p) for p in sorted(home.glob(".venvs/*/bin/python"))),
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
    ]
    seen, out = set(), []
    for c in cands:
        if c and c not in seen and Path(c).exists():
            seen.add(c)
            out.append(c)
    return out


def _has_fitz(exe: str) -> bool:
    try:
        return subprocess.run([exe, "-c", "import fitz"],
                              capture_output=True, timeout=25).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def find_interpreter() -> str | None:
    """The remembered interpreter if it still works, else the first that does."""
    try:
        saved = CONF.read_text().strip()
        if saved and Path(saved).exists() and _has_fitz(saved):
            return saved
    except Exception:  # noqa: BLE001
        pass
    for exe in _interpreters():
        if _has_fitz(exe):
            try:
                CONF.write_text(exe)
            except Exception:  # noqa: BLE001
                pass
            return exe
    return None


class Renderer:
    """One warm worker. All calls serialised — rendering is CPU-bound anyway."""

    CACHE_MAX = 90  # rendered pages

    def __init__(self):
        self.exe = find_interpreter()
        self.proc = None
        self.lock = threading.Lock()
        self.loaded: set[str] = set()
        self.cache: OrderedDict[tuple, bytes] = OrderedDict()

    @property
    def available(self) -> bool:
        return self.exe is not None

    def _ensure(self):
        if self.proc is None or self.proc.poll() is not None:
            self.loaded.clear()
            self.proc = subprocess.Popen(
                [self.exe, "-c", WORKER],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )

    def _call(self, req: dict, blob: bytes = b"") -> tuple[dict, bytes]:
        self._ensure()
        head = json.dumps(req).encode()
        self.proc.stdin.write(struct.pack("<II", len(head), len(blob)))
        self.proc.stdin.write(head)
        self.proc.stdin.write(blob)
        self.proc.stdin.flush()
        raw = self.proc.stdout.read(8)
        if len(raw) < 8:
            self.proc = None
            raise RuntimeError("render worker stopped")
        hlen, blen = struct.unpack("<II", raw)
        obj = json.loads(self.proc.stdout.read(hlen))
        body = self.proc.stdout.read(blen) if blen else b""
        if not obj.get("ok"):
            raise RuntimeError(obj.get("error", "render failed"))
        return obj, body

    def pages(self, key: str, data: bytes) -> list[list[float]]:
        """Page sizes in points. The UI reserves boxes with these so a lazily
        loaded page never shifts what is already on screen."""
        with self.lock:
            obj, _ = self._call({"cmd": "open", "key": key}, data)
            self.loaded.add(key)
            return obj["pages"]

    def text(self, key: str, data: bytes) -> str:
        """The document's text layer, for the removed/added comparison."""
        with self.lock:
            if key not in self.loaded:
                self._call({"cmd": "open", "key": key}, data)
                self.loaded.add(key)
            _, body = self._call({"cmd": "text", "key": key})
            return body.decode("utf-8", "replace")

    def page_png(self, key: str, data: bytes, n: int, zoom: float = 1.6) -> bytes:
        ck = (key, n, round(zoom, 2))
        with self.lock:
            hit = self.cache.get(ck)
            if hit is not None:
                self.cache.move_to_end(ck)
                return hit
            if key not in self.loaded:
                self._call({"cmd": "open", "key": key}, data)
                self.loaded.add(key)
            _, png = self._call({"cmd": "page", "key": key, "n": n, "zoom": zoom})
            self.cache[ck] = png
            while len(self.cache) > self.CACHE_MAX:
                self.cache.popitem(last=False)
            return png
