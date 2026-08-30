#!/usr/bin/env python3
"""Read-only backends for the two sides of a review.

A side is anything that can list relative paths and hand back bytes: a local
directory, a zip, or an S3 prefix. Everything above this file works against
``Store`` alone, so adding a backend does not touch the pairing or the UI.
"""
from __future__ import annotations

import functools
import io
import subprocess
import threading
import zipfile
from collections import OrderedDict
from pathlib import Path

#: Extensions worth putting in front of a reviewer. Everything else in a
#: deliverable (databases, logs, manifests) is bookkeeping.
DOC_EXTS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".csv", ".tsv",
    ".txt", ".md", ".json", ".jsonl", ".xml", ".html", ".htm", ".eml",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg",
}

#: macOS zip cruft and VCS. Pipeline bookkeeping is caught by the underscore
#: rule below rather than listed, because every stage names its own.
NOISE_DIRS = {"__MACOSX", ".git", "__pycache__", ".DS_Store", "node_modules"}


def is_doc(path: str) -> bool:
    """True for something worth putting in front of a reviewer.

    A leading underscore on ANY segment means pipeline bookkeeping, not a
    document: ``_pii/`` and ``_profile/`` hold the run's own state, ``_state/``
    holds the exporter's checkpoints, and the VLM writes ``_<name>`` beside the
    image it redacted. Excluding them by shape rather than by a list means a
    stage that invents a new one is covered without a code change.

    Pointing a side AT such a folder still works — an s3:// prefix or a folder
    path is stripped before these relative paths are formed, so
    ``s3://bucket/_pii/output`` lists ``github/x.pdf``, not ``_pii/...``.
    """
    parts = Path(path).parts
    if any(p in NOISE_DIRS for p in parts):
        return False
    if any(p.startswith("_") or p.startswith(".") for p in parts):
        return False
    return Path(parts[-1] if parts else "").suffix.lower() in DOC_EXTS


class Store:
    """A listable, readable location. Subclasses implement _list and read."""

    kind = "?"
    #: Read-through cache. An S3 read is a network round trip and the viewer
    #: asks for the same bytes again every time you step back one document.
    CACHE_MAX = 16

    def __init__(self, spec: str):
        self.spec = spec
        self._cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._cache_lock = threading.Lock()

    def cached_read(self, path: str) -> bytes:
        with self._cache_lock:
            hit = self._cache.get(path)
            if hit is not None:
                self._cache.move_to_end(path)
                return hit
        data = self.read(path)
        with self._cache_lock:
            self._cache[path] = data
            while len(self._cache) > self.CACHE_MAX:
                self._cache.popitem(last=False)
        return data

    def prefetch(self, path: str) -> None:
        """Warm the cache off-thread. Failures are ignored on purpose."""
        def _run():
            try:
                self.cached_read(path)
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_run, daemon=True).start()

    @functools.cached_property
    def paths(self) -> list[str]:
        """Everything listed, minus obvious junk. No document filtering.

        Kept separate from :attr:`docs` because a side can be SELECTED by a
        folder the document filter would reject: an in-place run writes its
        output to ``_pii/output``, and filtering before the selector is applied
        left that side empty.
        """
        return sorted(
            p for p in self._list()
            if not any(seg in NOISE_DIRS for seg in Path(p).parts)
        )

    @functools.cached_property
    def docs(self) -> list[str]:
        return sorted(p for p in self.paths if is_doc(p))

    def _list(self) -> list[str]:
        raise NotImplementedError

    def read(self, path: str) -> bytes:
        raise NotImplementedError

    def size(self, path: str) -> int:
        """Bytes, for the size-order pairing pass. 0 when unknown."""
        return 0

    def __repr__(self) -> str:
        # The s3:// scheme already names the backend; prefixing it again read
        # as "s3:s3://bucket" in the header.
        return self.spec if self.kind == "s3" else f"{self.kind}:{self.spec}"


class DirStore(Store):
    kind = "dir"

    def __init__(self, spec: str):
        super().__init__(spec)
        self.root = Path(spec).expanduser().resolve()

    def _list(self):
        return [
            str(p.relative_to(self.root))
            for p in self.root.rglob("*")
            if p.is_file()
        ]

    def read(self, path: str) -> bytes:
        target = (self.root / path).resolve()
        # A listed path is always under root; resolve() then compare so a
        # crafted request cannot walk out of the tree being reviewed.
        if not target.is_relative_to(self.root):
            raise PermissionError(path)
        return target.read_bytes()

    def size(self, path: str) -> int:
        return (self.root / path).stat().st_size


class ZipStore(Store):
    kind = "zip"

    def __init__(self, spec: str):
        super().__init__(spec)
        self.path = Path(spec).expanduser().resolve()

    def _list(self):
        with zipfile.ZipFile(self.path) as z:
            return [n for n in z.namelist() if not n.endswith("/")]

    def read(self, path: str) -> bytes:
        # Opened per read rather than held: a ZipFile handle is not safe to
        # share across the server's threads.
        with zipfile.ZipFile(self.path) as z:
            return z.read(path)

    @functools.cached_property
    def _sizes(self) -> dict[str, int]:
        with zipfile.ZipFile(self.path) as z:
            return {i.filename: i.file_size for i in z.infolist()}

    def size(self, path: str) -> int:
        return self._sizes.get(path, 0)


class S3Store(Store):
    """An S3 prefix. Uses boto3 when importable, else the aws CLI.

    The CLI fallback exists so the tool runs under a bare system python with
    nothing installed — which is the interpreter most people will reach for.
    """

    kind = "s3"

    def __init__(self, spec: str, profile: str | None = None):
        super().__init__(spec)
        rest = spec[len("s3://"):].strip("/")
        self.bucket, _, self.prefix = rest.partition("/")
        self.profile = profile or None
        self._client = None
        self._size_map: dict[str, int] = {}
        try:
            import boto3  # noqa: PLC0415 -- optional, probed at construction

            session = boto3.Session(profile_name=self.profile) if self.profile \
                else boto3.Session()
            self._client = session.client("s3")
        except Exception:  # noqa: BLE001 -- fall back to the CLI
            self._client = None

    def _aws(self, *args: str) -> bytes:
        cmd = ["aws"] + (["--profile", self.profile] if self.profile else []) + list(args)
        done = subprocess.run(cmd, capture_output=True)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.decode()[:400] or "aws failed")
        return done.stdout

    def _list(self):
        base = f"{self.prefix}/" if self.prefix else ""
        if self._client is not None:
            keys, token = [], None
            while True:
                kw = {"Bucket": self.bucket, "Prefix": base}
                if token:
                    kw["ContinuationToken"] = token
                page = self._client.list_objects_v2(**kw)
                for o in page.get("Contents", []):
                    keys.append(o["Key"])
                    self._size_map[o["Key"]] = o.get("Size", 0)
                token = page.get("NextContinuationToken")
                if not token:
                    break
        else:
            out = self._aws("s3", "ls", f"s3://{self.bucket}/{base}", "--recursive")
            keys = []
            for line in out.decode().splitlines():
                bits = line.split(None, 3)
                if len(bits) == 4:
                    keys.append(bits[3])
                    self._size_map[bits[3]] = int(bits[2]) if bits[2].isdigit() else 0
        n = len(base)
        self._size_map = {k[n:]: v for k, v in self._size_map.items()}
        return [k[n:] for k in keys if k != base and not k.endswith("/")]

    def size(self, path: str) -> int:
        return self._size_map.get(path, 0)

    def read(self, path: str) -> bytes:
        key = f"{self.prefix}/{path}" if self.prefix else path
        if self._client is not None:
            buf = io.BytesIO()
            self._client.download_fileobj(self.bucket, key, buf)
            return buf.getvalue()
        return self._aws("s3", "cp", f"s3://{self.bucket}/{key}", "-")


def open_store(spec: str, profile: str | None = None) -> Store:
    """Pick a backend from the shape of ``spec``. Raises with a usable message."""
    spec = (spec or "").strip()
    if not spec:
        raise ValueError("give a folder, a .zip, or an s3:// URI")
    if spec.startswith("s3://"):
        return S3Store(spec, profile)
    p = Path(spec).expanduser()
    if p.is_dir():
        return DirStore(str(p))
    if p.is_file() and p.suffix.lower() == ".zip":
        return ZipStore(str(p))
    if not p.exists():
        raise FileNotFoundError(f"no such folder or file: {spec}")
    raise ValueError(f"not a folder, a .zip, or an s3:// URI: {spec}")
