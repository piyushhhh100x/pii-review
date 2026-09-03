#!/usr/bin/env python3
"""Pair every source document with its counterpart in the output.

Two problems make this more than a filename join.

*The layouts differ.* A source tree is ``<unit>/raw/<connector>/<file>`` while
the deliverable is ``<unit>/files/<connector>/<file>``, or ``_pii/output/...``,
or a flat prefix. So paths are normalised by dropping segments that describe
the LAYOUT rather than the document, and the pairing runs on what is left.

*The output's filenames are themselves scrubbed.* An exact-name join finds
only the files the pipeline left alone, which is the wrong half. So within a
folder the match cascades: exact name, then surviving-token overlap, then sole
survivor, then size order — each pair labelled with how it was made, because a
reviewer should be able to see which ones were inferred.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import PurePosixPath

from stores import is_doc

#: Segment names that describe where a file sits rather than what it is.
#: User-editable from the setup screen — this is a default, not a rule.
DEFAULT_IGNORE = [
    "raw", "files", "file", "output", "out", "input", "in",
    "redacted", "original", "originals", "source", "src", "data", "documents",
]


def segments(path: str) -> tuple[str, ...]:
    return PurePosixPath(path.replace("\\", "/")).parts


def normalise(path: str, ignore: set[str]) -> tuple[str, ...]:
    """Path with layout segments removed. The last segment is always kept."""
    parts = segments(path)
    if not parts:
        return parts
    body = tuple(p for p in parts[:-1] if p.lower() not in ignore)
    return body + (parts[-1],)


def find_variants(paths: list[str], ignore: set[str]) -> dict[str, int]:
    """Segment values that make one side hold two copies of the same document.

    A source download can carry both ``<unit>/raw/...`` and ``<unit>/files/...``
    — the same documents twice, one copy already rewritten. Normalising
    collapses them onto one key, so the collision is the signal. Returns the
    competing segment values and how many paths each accounts for, which is
    what the setup screen offers as a choice. Empty when there is no ambiguity.
    """
    by_key: dict[tuple, list[str]] = defaultdict(list)
    for p in paths:
        by_key[normalise(p, ignore)].append(p)

    counts: Counter[str] = Counter()
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        for p in group:
            extra = [s for s in segments(p)[:-1] if s.lower() in ignore]
            for s in extra:
                counts[s] += 1
    return dict(counts)


def _tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[^A-Za-z0-9]+", name.lower()) if len(t) > 1}


def _match_bucket(left: list[str], right: list[str], size_of) -> list[tuple]:
    """Pair up one folder's worth of files. Returns (l, r, how) triples."""
    out: list[tuple] = []
    L, R = list(left), list(right)

    # 1 — exact filename. These are the files the rewriter had no reason to
    # rename; taking them first stops a looser pass stealing one of them.
    by_base: dict[str, list[str]] = defaultdict(list)
    for r in R:
        by_base[segments(r)[-1]].append(r)
    for l in list(L):
        pool = by_base.get(segments(l)[-1])
        if pool:
            r = pool.pop(0)
            out.append((l, r, "exact"))
            L.remove(l)
            R.remove(r)

    # 2 — surviving-token overlap. A scrubbed name keeps its numeric prefix and
    # its tail ("610044998-Experian-CreditReport-CRVD-…" becomes
    # "610044998-Vanova Ventures Corp.-CreditReport-CRVD-…"), so the overlap is
    # large. A tie is left for a later pass rather than guessed.
    for l in list(L):
        want = _tokens(segments(l)[-1])
        scored = sorted(((len(want & _tokens(segments(r)[-1])), r) for r in R),
                        key=lambda t: -t[0])
        if scored and scored[0][0] >= 2 and (
            len(scored) == 1 or scored[0][0] > scored[1][0]
        ):
            r = scored[0][1]
            out.append((l, r, "fuzzy"))
            L.remove(l)
            R.remove(r)

    # 3 — sole survivor. One left on each side in the same folder is not a
    # guess; there is nothing else it could be. This is the case where the only
    # distinguishing token was the one that got replaced.
    if len(L) == 1 and len(R) == 1:
        out.append((L[0], R[0], "sole"))
        return out

    # 4 — nearest size. Redaction changes a document's bytes but not its order
    # of magnitude, so sizes still rank the same way. Labelled so a reviewer
    # knows this pair was inferred.
    if L and R:
        sizes = {r: size_of(r) for r in R}
        for l in sorted(L, key=lambda x: -size_of(x, left=True)):
            if not R:
                break
            want = size_of(l, left=True)
            r = min(R, key=lambda x: abs(sizes[x] - want))
            out.append((l, r, "size"))
            R.remove(r)
    return out


#: How deep a branch can be and still be called a "unit" -- the thing a run is
#: organised by (a customer, a user, a connector export). Runs come in
#: ``<unit>/raw`` and ``<org>/<unit>/...`` shapes, so one and two both occur.
#: Below this, an absent counterpart is a withheld document, not a unit the run
#: was never pointed at, and it belongs in the review.
_UNIT_DEPTH = 2


def out_of_scope(left_dirs, right_dirs):
    """Source branches the output covers NOTHING of, as ``{unit: [dirs]}``.

    A bucket can hold several runs' worth of export. Point at it and every
    document from every OTHER run reads as "nothing in the output for this
    document" -- 9,083 of 9,779 rows in the run this was written against, all
    of them from a unit the pipeline was simply never given. That buries the
    696 rows worth reviewing and looks exactly like a catastrophic failure.

    The test is deliberately blunt: walk down from the root, and at the first
    level where the output tree branches at all, a source branch whose name is
    absent there was not part of this run. Not "mostly absent" -- absent. One
    output file anywhere beneath a branch keeps the whole branch in, because
    then the run did reach it and everything missing under it is a finding.

    Depth-capped at ``_UNIT_DEPTH``. A whole connector dropped from a unit the
    run DID process is a defect the reviewer has to see, and it sits deeper
    than this.
    """
    prefixes = set()
    for d in right_dirs:
        for n in range(1, len(d) + 1):
            prefixes.add(d[:n])

    # Names the source uses at each level, so a level that was RENAMED can be
    # told apart from a branch that was skipped.
    siblings: dict[tuple, set] = {}
    for d in left_dirs:
        for n in range(len(d)):
            siblings.setdefault(d[:n], set()).add(d[n])

    dropped: dict[str, list] = {}
    for d in left_dirs:
        k = 0
        while k < len(d) and d[:k + 1] in prefixes:
            k += 1
        if k >= len(d) or k + 1 > _UNIT_DEPTH:
            continue
        # The output must actually branch here, or "absent" means nothing --
        # it just means the trees diverge at this point for both halves.
        if not any(len(pre) == k + 1 and pre[:k] == d[:k] for pre in prefixes):
            continue
        # A level the pipeline RENAMES is not a level it skipped. The identity
        # folder is itself redacted -- google_calendar/anirudh.trivedi@inc42.com
        # ships as google_calendar/cyniria.selridge@example.com -- so EVERY
        # source name at that level is absent from the output, and reading that
        # as "none of these were in the run" silently deleted whole apps from
        # the review. One matching sibling means the level's names carry over
        # and an absent one really was skipped; none matching means they were
        # all rewritten, and nothing here can be judged out of scope.
        here = siblings.get(d[:k], set())
        carried = {seg for seg in here
                   if any(len(pre) == k + 1 and pre[:k] == d[:k] and pre[k] == seg
                          for pre in prefixes)}
        if not carried:
            continue
        dropped.setdefault("/".join(d[:k + 1]), []).append(d)
    return dropped


def build(left_store, right_store, ignore=None, left_only=None, right_only=None,
          left_exclude=None, right_exclude=None):
    """Index one review.

    ``*_only`` keeps only paths carrying that folder name; ``*_exclude`` drops
    the ones that do. A source of "everything except the output folder" — the
    shape of an in-place run — is expressed as ``left_exclude=<output name>``.
    """
    ignore = {s.strip().lower() for s in (ignore or DEFAULT_IGNORE) if s.strip()}

    def after(path: str, sel: str | None) -> str:
        """The part of ``path`` below the selector segment."""
        if not sel:
            return path
        parts = segments(path)
        low = [s.lower() for s in parts]
        try:
            return "/".join(parts[low.index(sel.strip().lower()) + 1:])
        except ValueError:
            return path

    def keep(paths, only, exclude):
        out = list(paths)
        if only:
            want = only.strip().lower()
            out = [p for p in out if want in {s.lower() for s in segments(p)}]
        if exclude:
            drop = exclude.strip().lower()
            out = [p for p in out if drop not in {s.lower() for s in segments(p)}]
        # Document filtering runs AFTER the selector, on the part below it.
        # An in-place run's output lives under "_pii/output", and testing the
        # whole path would reject the very side that was just selected.
        return [p for p in out if is_doc(after(p, only))]

    L = keep(left_store.paths, left_only, left_exclude)
    R = keep(right_store.paths, right_only, right_exclude)

    lsize = getattr(left_store, "size", None)
    rsize = getattr(right_store, "size", None)

    def size_of(path, left=False):
        fn = lsize if left else rsize
        if fn is not None:
            try:
                return fn(path)
            except Exception:  # noqa: BLE001
                return 0
        return 0

    # Bucket both sides by normalised parent folder, so pairing only ever
    # compares files that sit in the same place in the two trees.
    # Whatever named the two sides is a layout segment by definition; without
    # this the two halves normalise to different keys and nothing pairs.
    ignore |= {s.strip().lower() for s in (left_only, right_only) if s}

    lb: dict[tuple, list[str]] = defaultdict(list)
    rb: dict[tuple, list[str]] = defaultdict(list)
    for p in L:
        lb[normalise(p, ignore)[:-1]].append(p)
    for p in R:
        rb[normalise(p, ignore)[:-1]].append(p)

    pairs: list[dict] = []
    used_r: set[str] = set()
    for folder, ls in lb.items():
        rs = [r for r in rb.get(folder, []) if r not in used_r]
        for l, r, how in _match_bucket(ls, rs, size_of):
            pairs.append({"left": l, "right": r, "how": how, "folder": "/".join(folder)})
            used_r.add(r)

    paired_l = {p["left"] for p in pairs}
    leftover_l = [p for p in L if p not in paired_l]
    leftover_r = [p for p in R if p not in used_r]


    # A last global pass on filename alone, for trees whose folder structure
    # does not correspond at all.
    #
    # Restricted to names that are UNIQUE on both sides. Paginated exports name
    # every shard "page_000001.jsonl", so an unrestricted global join happily
    # paired one connector's first page with an unrelated connector's first
    # page — 1,277 confident-looking pairs, all of them wrong. A name that
    # occurs once on each side cannot be that mistake.
    lcount = Counter(segments(p)[-1] for p in leftover_l)
    rcount = Counter(segments(p)[-1] for p in leftover_r)
    rby: dict[str, str] = {segments(p)[-1]: p for p in leftover_r}
    for l in list(leftover_l):
        base = segments(l)[-1]
        if lcount[base] == 1 and rcount.get(base) == 1:
            r = rby[base]
            pairs.append({"left": l, "right": r, "how": "name",
                          "folder": "/".join(normalise(l, ignore)[:-1])})
            leftover_l.remove(l)
            leftover_r.remove(r)

    # Only now, once every route to a counterpart has been tried: source
    # branches the output covers nothing of were never in this run. Held back
    # from the index rather than dropped silently -- the caller reports them,
    # because "this bucket holds another run too" is worth knowing and is not
    # the same fact as "the pipeline withheld a document".
    #
    # After the name pass, not before. Run first, this discarded the very
    # paths that pass exists to rescue: a tree whose folders do not correspond
    # at all looks branch-for-branch uncovered right up until the filenames
    # match it up.
    scope = out_of_scope({normalise(p, ignore)[:-1] for p in leftover_l}, set(rb))
    skipped = {d for dirs in scope.values() for d in dirs}
    off = [p for p in leftover_l if normalise(p, ignore)[:-1] in skipped]
    leftover_l = [p for p in leftover_l if normalise(p, ignore)[:-1] not in skipped]

    pairs.sort(key=lambda p: normalise(p["left"], ignore))
    return {
        "pairs": pairs,
        "unmatched_left": sorted(leftover_l),
        "unmatched_right": sorted(leftover_r),
        "out_of_scope": sorted(off),
        "out_of_scope_units": {u: sum(1 for p in off
                                      if normalise(p, ignore)[:-1] in set(dirs))
                               for u, dirs in scope.items()},
        "counts": {
            "left_docs": len(L), "right_docs": len(R),
            "by_how": dict(Counter(p["how"] for p in pairs)),
        },
    }


#: Folder names that mark the two halves of a run. A parent that contains one
#: of each is self-describing, which is the common case: the reviewer points at
#: the export root and the split is read off the tree rather than typed in.
SOURCE_MARKERS = ["raw", "source", "sources", "original", "originals",
                  "input", "inputs", "in", "src", "before", "pre"]
OUTPUT_MARKERS = ["redacted", "output", "outputs", "out", "processed",
                  "scrubbed", "masked", "clean", "after", "post", "sanitized",
                  "sanitised", "anonymized", "anonymised"]


#: ``_pii`` is where the pipeline writes its output in an in-place run, so it
#: belongs with the output names even though it reads as bookkeeping.
OUTPUT_MARKERS = OUTPUT_MARKERS + ["_pii", "pii"]


#: What the pipeline appends to a run root when it writes in place, matching
#: ``pii_pipeline.output_prefix`` in the exporter config. Longest first, so
#: ``_pii/output`` is stripped whole rather than one segment at a time.
OUTPUT_PREFIXES = ("_pii/output", "_pii")


def run_root(spec: str) -> str:
    """The run root behind a location that points INTO the output half.

    A reviewer copies a location out of the console while looking at the
    result, so what lands in the box is ``.../_pii/output`` -- the half that
    has no source to compare against. Opened literally that yields a review
    with an empty left pane for every row, which reads as "the pipeline
    dropped everything" rather than "you pointed one level too deep".

    Only the pipeline's own suffix is stripped, and only from the end. A run
    that genuinely lives in a folder called ``output`` is untouched, because
    the exporter writes ``_pii/output`` and nothing else.
    """
    spec = (spec or "").rstrip("/")
    for suffix in OUTPUT_PREFIXES:
        tail = "/" + suffix
        if spec.lower().endswith(tail.lower()):
            trimmed = spec[: -len(tail)]
            # Never strip back to a bare scheme or an empty path: "s3://b/_pii"
            # has a root, "s3://_pii" does not.
            if trimmed and not trimmed.endswith((":/", "s3:/", "/")):
                return trimmed
    return spec


def layout_names(paths: list[str], max_siblings: int = 8):
    """Segment names that describe the tree's LAYOUT, with their file counts.

    A layout level is one where every path takes one of a handful of turns:
    ``<unit>/{raw,files,redacted}/…`` has three. A content level does not —
    thirty customer ids or forty connector names sit at the same depth. So a
    depth qualifies when its distinct names are few, which separates the two
    without knowing any of the names in advance.
    """
    by_depth: dict[int, Counter] = defaultdict(Counter)
    for p in paths:
        for depth, seg in enumerate(segments(p)[:-1]):
            if depth <= 4:
                by_depth[depth][seg.lower()] += 1
    names: Counter[str] = Counter()
    depth_of: dict[str, int] = {}
    for depth, counts in sorted(by_depth.items()):
        if len(counts) <= max_siblings:
            names.update(counts)
            for n in counts:
                depth_of.setdefault(n, depth)
    # A segment EVERY path carries cannot divide anything. Without this the
    # shared root of "u/raw/..." and "u/redacted/..." scored as the best
    # output folder, because it "covers" the source almost perfectly. The
    # test is exact rather than a threshold: at 95% it also threw away a real
    # source folder that happened to hold all but one of the documents.
    total = len(paths)
    return {n: c for n, c in names.items() if c < total}, depth_of


def autosplit(paths: list[str]) -> dict:
    """Best guess at how one parent divides into source and output.

    Returned WITH every candidate and the file count behind each, because the
    guess is regularly wrong in a way only the operator can see: a download can
    carry ``raw/`` (every document), ``files/`` (rewritten in place) and
    ``redacted/`` (populated for one customer in thirty). Name alone cannot
    rank those, so the guess is shown rather than applied silently.

    A source of ``None`` means "everything that is not the output", which is
    the shape of an in-place run: the tree is the source and ``_pii/output``
    is the result.
    """
    names, depth = layout_names(paths)
    marker = [m for m in (SOURCE_MARKERS + OUTPUT_MARKERS) if m in names]
    other = [n for n in sorted(names, key=lambda n: -names[n]) if n not in marker]
    keep = marker + other[:4]
    options = [{"name": n, "files": names[n]} for n in
               sorted(keep, key=lambda n: -names[n])]

    def best(markers):
        # Shallowest first: that is where the tree actually forks. With
        # "_pii/output/..." both names are output markers and equally common,
        # but "_pii" is the fork and "output" merely sits under it.
        hits = [m for m in markers if m in names]
        return min(hits, key=lambda m: (depth[m], -names[m])) if hits else None

    source = best(SOURCE_MARKERS)

    if source:
        # A run redacts nearly every document it is given, so the output folder
        # is the sibling holding a comparable NUMBER of files. Coverage leads
        # and the name only breaks ties: this tree has both "files/" (1,731 of
        # 1,737) and "redacted/" (82), and picking on name alone chose the one
        # that was populated for a single customer — every document then read
        # as missing.
        def score(name: str) -> float:
            a, b = names[name], names[source]
            cover = min(a, b) / max(a, b, 1)
            return cover + (0.25 if name in OUTPUT_MARKERS else 0.0)

        rest = [o["name"] for o in options if o["name"] != source]
        output = max(rest, key=score) if rest else None
    else:
        # No source folder means an in-place run: the tree IS the source and
        # the output sits under a named folder. Here the name is all there is,
        # and coverage would pick the largest connector.
        output = best(OUTPUT_MARKERS)

    warn = None
    if output is None:
        warn = "could not tell which folder holds the output — pick it below"
    elif source and names.get(output, 0) < names.get(source, 1) * 0.5:
        warn = (f"{output}/ holds {names[output]} of the {names[source]} "
                f"documents in {source}/ — check this is the pair you want")
    return {"options": options, "source": source, "output": output, "warn": warn}
