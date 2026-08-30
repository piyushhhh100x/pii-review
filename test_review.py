#!/usr/bin/env python3
"""Tests. Run with: python3 test_review.py

Pairing and layout detection run on synthetic trees, so they check the logic
rather than one batch's quirks. The end-to-end tests boot a real server and
skip themselves when no sample data is on this machine.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from pathlib import Path

import pairing
import render
import review
import stores

#: A real export to run the end-to-end tests against. Point PII_REVIEW_SAMPLE
#: at one holding <unit>/raw/... and <unit>/files/...; without it those tests
#: skip and the rest still run.
SAMPLE = Path(os.environ.get("PII_REVIEW_SAMPLE", "~/Downloads/sample-export")).expanduser()


def tree(root: Path, files: dict[str, bytes]):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


class Docs(unittest.TestCase):
    def test_bookkeeping_is_not_a_document(self):
        for p in ("_pii/logs/a.jsonl", "u/_profile/m.json", "x/_state/done/y.json",
                  "img/_scan.png", "__MACOSX/a.pdf", ".DS_Store"):
            self.assertFalse(stores.is_doc(p), p)

    def test_real_documents_are(self):
        for p in ("experian/report.pdf", "a/b/statement.csv", "x.png"):
            self.assertTrue(stores.is_doc(p), p)

    def test_unknown_extensions_are_skipped(self):
        self.assertFalse(stores.is_doc("a/pii_mappings.db"))


class Layout(unittest.TestCase):
    def test_layout_segments_drop_out_of_the_key(self):
        ign = set(pairing.DEFAULT_IGNORE)
        self.assertEqual(pairing.normalise("u1/raw/exp/a.pdf", ign),
                         pairing.normalise("u1/files/exp/a.pdf", ign))

    def test_a_content_level_is_not_a_layout_level(self):
        # Thirty customer ids at one depth is content; two names is layout.
        paths = ([f"cust{n}/raw/c/f.pdf" for n in range(30)]
                 + [f"cust{n}/files/c/f.pdf" for n in range(30)])
        names, _ = pairing.layout_names(paths)
        self.assertIn("raw", names)
        self.assertIn("files", names)
        self.assertNotIn("cust0", names)

    def test_a_segment_every_path_shares_cannot_split_anything(self):
        paths = ([f"root/raw/f{n}.pdf" for n in range(20)]
                 + [f"root/files/f{n}.pdf" for n in range(20)])
        names, _ = pairing.layout_names(paths)
        self.assertNotIn("root", names)

    def test_output_is_chosen_by_coverage_not_by_name(self):
        # "redacted" sounds more like output than "files" but holds almost
        # nothing; a run redacts nearly everything it is given.
        paths = ([f"u/raw/f{n}.pdf" for n in range(100)]
                 + [f"u/files/f{n}.pdf" for n in range(98)]
                 + ["u/redacted/f0.pdf"])
        g = pairing.autosplit(paths)
        self.assertEqual(g["source"], "raw")
        self.assertEqual(g["output"], "files")

    def test_in_place_runs_split_into_everything_else(self):
        paths = ([f"github/f{n}.pdf" for n in range(50)]
                 + [f"_pii/output/github/f{n}.pdf" for n in range(50)])
        g = pairing.autosplit(paths)
        self.assertIsNone(g["source"])
        self.assertEqual(g["output"], "_pii")

    def test_thin_output_is_warned_about(self):
        paths = [f"u/raw/f{n}.pdf" for n in range(100)] + ["u/redacted/f0.pdf"]
        self.assertTrue(pairing.autosplit(paths)["warn"])


class Pairing(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def build(self, files, **kw):
        tree(self.dir, files)
        st = stores.open_store(str(self.dir))
        return pairing.build(st, st, **kw)

    def test_exact_names_pair_first(self):
        idx = self.build({"u/raw/c/a.pdf": b"a", "u/out/c/a.pdf": b"A"},
                         left_only="raw", right_only="out")
        self.assertEqual([p["how"] for p in idx["pairs"]], ["exact"])

    def test_a_scrubbed_name_pairs_on_surviving_tokens(self):
        idx = self.build({
            "u/raw/c/610044998-Experian-CreditReport-CRVD-471.pdf": b"a",
            "u/out/c/610044998-Vanova Ventures-CreditReport-CRVD-471.pdf": b"A",
        }, left_only="raw", right_only="out")
        self.assertEqual([p["how"] for p in idx["pairs"]], ["fuzzy"])

    def test_the_only_file_left_in_a_folder_is_not_a_guess(self):
        idx = self.build({"u/raw/c/Experian.pdf": b"a",
                          "u/out/c/Vanova Ventures Corp.pdf": b"A"},
                         left_only="raw", right_only="out")
        self.assertEqual([p["how"] for p in idx["pairs"]], ["sole"])

    def test_a_source_with_no_counterpart_is_reported_not_dropped(self):
        idx = self.build({"u/raw/c/a.pdf": b"a", "u/raw/c/b.pdf": b"b",
                          "u/out/c/a.pdf": b"A"},
                         left_only="raw", right_only="out")
        self.assertEqual(len(idx["pairs"]), 1)
        self.assertEqual(len(idx["unmatched_left"]), 1)

    def test_generic_names_do_not_pair_across_unrelated_folders(self):
        # Paginated exports name every shard the same; an unrestricted global
        # join paired one connector's first page with another's.
        idx = self.build({
            "u/raw/aa/page_000001.jsonl": b"a", "u/raw/bb/page_000001.jsonl": b"b",
            "u/out/cc/page_000001.jsonl": b"A", "u/out/dd/page_000001.jsonl": b"B",
        }, left_only="raw", right_only="out")
        self.assertEqual(idx["pairs"], [])

    def test_a_unique_name_still_pairs_across_folders(self):
        idx = self.build({"u/raw/aa/only-one.pdf": b"a", "u/out/zz/only-one.pdf": b"A"},
                         left_only="raw", right_only="out")
        self.assertEqual([p["how"] for p in idx["pairs"]], ["name"])

    def test_output_selected_below_an_underscore_folder(self):
        # The document filter must run below the selector, or selecting
        # "_pii" leaves that side empty.
        idx = self.build({"github/a.pdf": b"a", "_pii/output/github/a.pdf": b"A"},
                         right_only="_pii", left_exclude="_pii")
        self.assertEqual([p["how"] for p in idx["pairs"]], ["exact"])


class Zip(unittest.TestCase):
    def test_a_zip_is_read_in_place(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        z = d / "out.zip"
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("u/files/c/a.pdf", "hello")
            f.writestr("u/files/_pii/log.jsonl", "x")
        st = stores.open_store(str(z))
        self.assertEqual(st.docs, ["u/files/c/a.pdf"])
        self.assertEqual(st.read("u/files/c/a.pdf"), b"hello")
        self.assertEqual(st.size("u/files/c/a.pdf"), 5)


class Marks(unittest.TestCase):
    def test_older_shapes_survive(self):
        got = review._migrate({
            "a": "flag",
            "b": {"v": "ok", "note": "surname on p2"},
            "c": {"viewed": True, "reviewed": False, "comments": ["x"]},
        })
        self.assertTrue(got["a"]["reviewed"])
        self.assertEqual(got["b"]["comments"], ["surname on p2"])
        self.assertFalse(got["c"]["reviewed"])
        self.assertEqual(got["c"]["comments"], ["x"])


class Render(unittest.TestCase):
    def test_a_pdf_renders_and_caches(self):
        r = render.Renderer()
        if not r.available:
            self.skipTest("no interpreter with PyMuPDF")
        pdf = next(SAMPLE.rglob("*/raw/**/*.pdf"), None) if SAMPLE.exists() else None
        if pdf is None:
            self.skipTest("no sample PDF")
        data = pdf.read_bytes()
        pages = r.pages("t", data)
        self.assertGreater(len(pages), 0)
        self.assertEqual(r.page_png("t", data, 0)[:4], b"\x89PNG")
        self.assertIn("t", r.loaded)


class EndToEnd(unittest.TestCase):
    """Boots the real server against the sample batch."""

    @classmethod
    def setUpClass(cls):
        if not SAMPLE.exists():
            raise unittest.SkipTest("no sample batch on this machine")
        cls.port = 8791
        review.MARKS = Path(tempfile.mkdtemp()) / "marks.json"
        cls.srv = review.Server(("127.0.0.1", cls.port), review.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        review.open_review(root=str(SAMPLE), source="raw", output="files")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def get(self, path):
        return urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=90).read()

    def post(self, path, obj):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=json.dumps(obj).encode(), method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=90).read())

    def test_boot_carries_the_whole_session(self):
        b = json.loads(self.get("/api/boot"))
        self.assertTrue(b["ready"])
        # A reload used to come back not knowing what it was comparing.
        self.assertEqual(b["source"], "raw")
        self.assertEqual(b["right_short"], "files")
        self.assertGreater(len(b["pairs"]), 100)

    def test_both_sides_of_a_pair_are_served_and_differ(self):
        b = json.loads(self.get("/api/boot"))
        p = next(x for x in b["pairs"] if x["right"] and x["label"].endswith(".pdf"))
        a = self.get(f"/doc/left/{p['id']}")
        z = self.get(f"/doc/right/{p['id']}")
        self.assertEqual(a[:4], b"%PDF")
        self.assertEqual(z[:4], b"%PDF")

    def test_pages_render_as_png(self):
        b = json.loads(self.get("/api/boot"))
        p = next(x for x in b["pairs"] if x["right"] and x["label"].endswith(".pdf"))
        meta = json.loads(self.get(f"/api/doc/left/{p['id']}"))
        if meta["kind"] != "pdf":
            self.skipTest("rendering unavailable")
        self.assertGreater(len(meta["pages"]), 0)
        self.assertEqual(self.get(f"/page/left/{p['id']}/0.png")[:4], b"\x89PNG")

    def test_metrics_report_what_changed(self):
        b = json.loads(self.get("/api/boot"))
        p = next(x for x in b["pairs"] if x["right"] and x["label"].endswith(".pdf"))
        m = json.loads(self.get(f"/api/metrics/{p['id']}"))
        self.assertIn("identical", m)
        self.assertIn("removed", m)

    def test_a_record_round_trips_to_disk(self):
        b = json.loads(self.get("/api/boot"))
        k = b["pairs"][0]["left"]
        self.post("/api/mark", {"key": k, "rec": {"viewed": True, "reviewed": True,
                                                  "comments": ["one", "two"]}})
        saved = json.loads(review.MARKS.read_text())
        rec = next(iter(saved.values()))[k]
        self.assertEqual(rec["comments"], ["one", "two"])
        self.post("/api/mark", {"key": k, "rec": {"viewed": False, "reviewed": False,
                                                  "comments": []}})
        saved = json.loads(review.MARKS.read_text())
        self.assertNotIn(k, next(iter(saved.values())))

    def test_a_missing_counterpart_has_no_output_side(self):
        b = json.loads(self.get("/api/boot"))
        miss = [x for x in b["pairs"] if x["how"] == "missing"]
        if not miss:
            self.skipTest("this batch has no withheld files")
        self.assertIsNone(miss[0]["right"])
        m = json.loads(self.get(f"/api/metrics/{miss[0]['id']}"))
        self.assertTrue(m["missing"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
