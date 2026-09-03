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


class Formatting(unittest.TestCase):
    """Structured formats get ONE shape, so the two panes can be compared.

    The pipeline reserialises what it rewrites: a source record reads
    ``{"a":1}`` and its own output reads ``{"a": 1}``. Same data, different
    spacing, so the panes wrapped differently and scroll sync lined up
    unrelated lines. Formatting both sides here is what fixes that.
    """

    def test_the_two_spellings_of_one_record_format_identically(self):
        compact = b'{"id":"10022","author":{"emailAddress":"a@b.com"}}'
        spaced = b'{"id": "10022", "author": {"emailAddress": "a@b.com"}}'
        self.assertEqual(review.view("a.json", compact)["html"],
                         review.view("a.json", spaced)["html"])

    def test_json_is_indented_and_highlighted(self):
        out = review.view("a.json", b'{"emailAddress":"a@b.com","n":3,"ok":true}')
        self.assertEqual(out["kind"], "text")
        self.assertIn('<span class=jk>"emailAddress"</span>', out["html"])
        self.assertIn('<span class=js>"a@b.com"</span>', out["html"])
        self.assertIn('<span class=jn>3</span>', out["html"])
        self.assertIn('<span class=jb>true</span>', out["html"])

    def test_a_colon_inside_a_value_is_not_read_as_a_key(self):
        # The corpus is full of "content":"https://host/x?a=b:c". A regex
        # highlighter marks that inner colon as a separator; walking the
        # parsed object cannot.
        out = review.view("a.json", b'{"content":"https://h/x?a=b:c"}')
        self.assertIn('<span class=js>"https://h/x?a=b:c"</span>', out["html"])
        self.assertEqual(out["html"].count("class=jk"), 1)

    def test_jsonl_is_one_numbered_record_per_row(self):
        data = b'{"a":1}\n{"a":2}\n\n{"a":3}\n'
        out = review.view("a.jsonl", data)
        self.assertEqual(out["kind"], "records")
        self.assertEqual(out["html"].count('class=rec>'), 3)   # blank line skipped
        self.assertIn('<div class=recn>3</div>', out["html"])

    def test_a_broken_jsonl_line_is_shown_not_swallowed(self):
        # "the rewriter emitted broken JSON" is itself the finding.
        out = review.view("a.jsonl", b'{"a":1}\nnot json at all\n')
        self.assertEqual(out["kind"], "records")
        self.assertIn("not json at all", out["html"])
        self.assertIn("class=jbad", out["html"])

    def test_unparseable_json_falls_back_to_raw_text(self):
        out = review.view("a.json", b"{not json")
        self.assertEqual(out["kind"], "text")
        self.assertIn("{not json", out["html"])

    def test_xml_is_indented(self):
        out = review.view("a.xml", b"<a><b>x</b><c>y</c></a>")
        self.assertEqual(out["kind"], "text")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out["html"])
        self.assertNotIn("\n\n", out["html"])

    def test_a_huge_jsonl_is_capped_and_says_so(self):
        data = b"\n".join(b'{"a":%d}' % n for n in range(900))
        out = review.view("a.jsonl", data)
        self.assertIn("more records not shown", out["html"])
        self.assertLess(out["html"].count('class=rec>'), 900)


class RenamedLevels(unittest.TestCase):
    """A level the pipeline renames is not a level it skipped.

    The identity folder is itself redacted, so every source name at that level
    is absent from the output. Read as "none of these were in the run", that
    silently deleted whole apps from the review -- google_calendar went from
    26 documents to 1.
    """

    def test_a_renamed_identity_level_drops_nothing(self):
        left = {("google_calendar", "anirudh.trivedi@inc42.com", "events"),
                ("google_calendar", "amit.kumar@inc42.com", "events")}
        right = {("google_calendar", "cyniria.selridge@example.com", "events")}
        self.assertEqual(pairing.out_of_scope(left, right), {})

    def test_a_genuinely_skipped_unit_is_still_caught(self):
        # Names carry over here, so an absent one really was not in the run.
        left = {("jira", "nx-004", "shared"), ("jira", "nx-005", "shared")}
        right = {("jira", "nx-004", "shared")}
        self.assertEqual(list(pairing.out_of_scope(left, right)), ["jira/nx-005"])

    def test_one_surviving_sibling_is_enough_to_judge_the_rest(self):
        left = {("app", "a"), ("app", "b"), ("app", "c")}
        right = {("app", "a")}
        self.assertEqual(sorted(pairing.out_of_scope(left, right)),
                         ["app/b", "app/c"])


class AlignBySize(unittest.TestCase):
    """Same-shape folders told apart by how many bytes are in them."""

    def build(self, files, **kw):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        tree(Path(d), files)
        st = stores.open_store(d)
        return pairing.build(st, st, **kw)

    def test_two_people_with_the_same_shape_are_matched_by_size(self):
        # Both have one page. Only the byte counts say which is which.
        idx = self.build({
            "u/raw/ann/page_000001.jsonl": b"a" * 900,
            "u/raw/bob/page_000001.jsonl": b"b" * 100,
            "u/out/xxx/page_000001.jsonl": b"A" * 890,   # ann
            "u/out/yyy/page_000001.jsonl": b"B" * 104,   # bob
        }, left_only="raw", right_only="out")
        got = {p["left"].split("/")[2]: p["right"].split("/")[2] for p in idx["pairs"]}
        self.assertEqual(got, {"ann": "xxx", "bob": "yyy"})

    def test_sizes_too_close_to_call_are_left_unpaired(self):
        # No margin between the candidates, so there is no evidence. Pairing
        # the wrong two puts one person's source beside another's output and
        # every difference then reads as a leak.
        idx = self.build({
            "u/raw/ann/page_000001.jsonl": b"a" * 500,
            "u/raw/bob/page_000001.jsonl": b"b" * 500,
            "u/out/xxx/page_000001.jsonl": b"A" * 500,
            "u/out/yyy/page_000001.jsonl": b"B" * 500,
        }, left_only="raw", right_only="out")
        self.assertEqual(idx["pairs"], [])

    def test_a_wildly_different_size_is_not_forced(self):
        # Three folders a side, so shape alone cannot pick. ann has no
        # plausible counterpart by size and stays unpaired while the others
        # match.
        idx = self.build({
            "u/raw/ann/page_000001.jsonl": b"a" * 100000,
            "u/raw/bob/page_000001.jsonl": b"b" * 500,
            "u/raw/cid/page_000001.jsonl": b"c" * 20,
            "u/out/xxx/page_000001.jsonl": b"A" * 505,   # bob
            "u/out/yyy/page_000001.jsonl": b"B" * 21,    # cid
        }, left_only="raw", right_only="out")
        got = {p["left"].split("/")[2]: p["right"].split("/")[2] for p in idx["pairs"]}
        self.assertEqual(got, {"bob": "xxx", "cid": "yyy"})
        self.assertNotIn("ann", got)

    def test_one_folder_each_side_still_matches_on_shape_alone(self):
        # Nothing to be ambiguous about, so size is not consulted.
        idx = self.build({"u/raw/ann/only.pdf": b"a" * 900,
                          "u/out/xxx/only.pdf": b"A" * 20},
                         left_only="raw", right_only="out")
        self.assertEqual(len(idx["pairs"]), 1)


class PerReviewerSample(unittest.TestCase):
    """Two people on one run should not spend the day on the same hundred."""

    def rows(self, n):
        return [{"label": f"gmail/f{i:04}.txt", "id": i} for i in range(n)]

    def test_the_same_machine_gets_the_same_files_every_time(self):
        # A refresh, a restart or a fresh checkout must put the same files
        # back. Verdicts are keyed by path; a sample that moved would strand
        # yesterday's review.
        a, _ = review._thin(self.rows(500), 20, "laptop-A")
        b, _ = review._thin(self.rows(500), 20, "laptop-A")
        self.assertEqual([r["label"] for r in a], [r["label"] for r in b])

    def test_two_machines_get_different_files(self):
        a, _ = review._thin(self.rows(500), 20, "laptop-A")
        b, _ = review._thin(self.rows(500), 20, "laptop-B")
        A, B = {r["label"] for r in a}, {r["label"] for r in b}
        self.assertNotEqual(A, B)
        # Not merely different -- barely overlapping, or two reviewers buy
        # very little coverage between them.
        self.assertLess(len(A & B), 8)

    def test_a_shared_seed_reproduces_a_colleagues_sample(self):
        # For "show me exactly what you were looking at".
        a, _ = review._thin(self.rows(500), 20, "shared")
        b, _ = review._thin(self.rows(500), 20, "shared")
        self.assertEqual([r["label"] for r in a], [r["label"] for r in b])

    def test_the_salt_is_written_once_and_reused(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        old = review.SALT_FILE
        review.SALT_FILE = Path(d) / "salt"
        self.addCleanup(setattr, review, "SALT_FILE", old)
        first = review.reviewer_salt()
        self.assertTrue(review.SALT_FILE.exists())
        self.assertEqual(first, review.reviewer_salt())

    def test_an_override_wins_over_the_stored_salt(self):
        self.assertEqual(review.reviewer_salt("someone-else"), "someone-else")


class Mappings(unittest.TestCase):
    """The run's substitution table, readable without a sqlite shell."""

    def db(self, rows, table="mappings"):
        import sqlite3
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = str(Path(d) / "pii_mappings.db")
        con = sqlite3.connect(path)
        con.execute(f"create table {table} (id integer primary key, original text, "
                    "attribute_type text, replacement text, confidence real)")
        con.executemany(f"insert into {table} (original, attribute_type, replacement) "
                        "values (?,?,?)", rows)
        con.commit()
        con.close()
        return path

    def test_it_reads_the_table(self):
        path = self.db([("a@b.com", "email", "x@y.com"),
                        ("Acme", "company_name", "Zorp")])
        out = review.read_mappings(path)
        self.assertEqual(out["count"], 2)
        # Grouped by type, so the panel reads as a table of kinds rather than
        # of insertion order.
        self.assertEqual([r["attribute_type"] for r in out["rows"]],
                         ["company_name", "email"])

    def test_a_row_the_run_never_replaced_survives(self):
        # replacement NULL is itself the finding -- it was found and not acted
        # on -- so it must not be dropped on the way to the panel.
        path = self.db([("gmail.com", "company_name", None)])
        self.assertIsNone(review.read_mappings(path)["rows"][0]["replacement"])

    def test_a_database_with_no_mappings_table_says_so(self):
        path = self.db([("a", "b", "c")], table="something_else")
        with self.assertRaises(ValueError):
            review.read_mappings(path)

    def test_a_missing_file_is_a_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            review.read_mappings("/tmp/definitely-not-here.db")

    def test_it_is_found_beside_the_output_when_the_run_shipped_one(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        tree(Path(d), {"github/a.pdf": b"a", "_pii/pii_mappings.db": b"x"})
        self.assertEqual(review.find_mappings(stores.open_store(d)),
                         "_pii/pii_mappings.db")

    def test_no_database_is_not_an_error(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        tree(Path(d), {"github/a.pdf": b"a"})
        self.assertIsNone(review.find_mappings(stores.open_store(d)))


class Thinning(unittest.TestCase):
    """A hundred pairs per app, chosen at random, stable across reopens."""

    def rows(self, app, n):
        return [{"label": f"{app}/f{i:04}.txt", "id": i} for i in range(n)]

    def test_a_small_app_is_left_alone(self):
        kept, dropped = review._thin(self.rows("gmail", 30), 100, "s")
        self.assertEqual(len(kept), 30)
        self.assertEqual(dropped, {})

    def test_a_big_app_is_cut_to_the_cap(self):
        kept, dropped = review._thin(self.rows("gmail", 900), 100, "s")
        self.assertEqual(len(kept), 100)
        self.assertEqual(dropped, {"gmail": 800})

    def test_the_budget_is_per_app_not_per_run(self):
        # One budget spent top-down goes entirely to whichever app sorts
        # first and shows none of the rest.
        rows = self.rows("gmail", 500) + self.rows("google_drive", 500)
        kept, _ = review._thin(rows, 100, "s")
        got = {}
        for r in kept:
            got[r["label"].split("/")[0]] = got.get(r["label"].split("/")[0], 0) + 1
        self.assertEqual(got, {"gmail": 100, "google_drive": 100})

    def test_it_is_not_just_the_first_hundred(self):
        # S3 hands back keys in sort order, so the head of the list is always
        # the same corner of the same export.
        kept, _ = review._thin(self.rows("gmail", 900), 100, "s")
        self.assertNotEqual([r["label"] for r in kept],
                            [r["label"] for r in self.rows("gmail", 900)[:100]])

    def test_the_same_run_thins_to_the_same_files(self):
        # Verdicts are keyed by path. A sample that reshuffled on reopen would
        # strand yesterday's review against files nobody can see today.
        a, _ = review._thin(self.rows("gmail", 900), 100, "s")
        b, _ = review._thin(self.rows("gmail", 900), 100, "s")
        self.assertEqual([r["label"] for r in a], [r["label"] for r in b])

    def test_zero_means_no_thinning(self):
        kept, dropped = review._thin(self.rows("gmail", 900), 0, "s")
        self.assertEqual(len(kept), 900)
        self.assertEqual(dropped, {})


class Planning(unittest.TestCase):
    """Several locations means several reviews, one browser tab each."""

    class Args:
        root = None; pair = None; left = None; right = None; label = None

    def plan(self, **kw):
        a = self.Args()
        for k, v in kw.items():
            setattr(a, k, v)
        return review.plan(a)

    def test_one_location_is_one_review(self):
        self.assertEqual(len(self.plan(root=["/tmp/a"])), 1)

    def test_three_locations_are_three_reviews(self):
        jobs = self.plan(root=["/tmp/a", "/tmp/b", "/tmp/c"])
        self.assertEqual([j["root"] for j in jobs], ["/tmp/a", "/tmp/b", "/tmp/c"])

    def test_each_review_gets_a_name_of_its_own(self):
        # Three identical tab titles is how a reviewer ends up marking the
        # wrong run as clean.
        jobs = self.plan(root=["s3://b/run-a", "s3://b/run-b"])
        self.assertEqual([j["label"] for j in jobs], ["run-a", "run-b"])

    def test_pair_is_repeatable_and_names_both_halves(self):
        jobs = self.plan(pair=[["s3://b/src", "s3://b/out"],
                               ["/x/raw", "/x/files"]])
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["left"], "s3://b/src")
        self.assertEqual(jobs[0]["right"], "s3://b/out")
        self.assertIn("src", jobs[0]["label"])
        self.assertIn("out", jobs[0]["label"])

    def test_roots_and_pairs_compose(self):
        jobs = self.plan(root=["/tmp/a"], pair=[["/x/raw", "/x/files"]])
        self.assertEqual(len(jobs), 2)

    def test_the_old_left_right_flags_still_work(self):
        jobs = self.plan(left="/x/raw", right="/x/files")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["right"], "/x/files")

    def test_nothing_asked_for_is_no_jobs(self):
        self.assertEqual(self.plan(), [])


class S3Urls(unittest.TestCase):
    """Every shape an S3 location arrives in must open.

    Nobody types ``s3://``. What is in the clipboard is whatever the console
    put in the address bar, and every one of these used to be rejected as
    "not a folder, a .zip, or an s3:// URI".
    """

    BUCKET = "test-pii-t2-nexus-saild1-1"

    def test_the_console_url_for_a_folder(self):
        u = (f"https://s3.console.aws.amazon.com/s3/buckets/{self.BUCKET}"
             "?region=ap-south-1&prefix=_pii/output/&bucketType=general")
        self.assertEqual(stores.parse_s3(u),
                         (f"s3://{self.BUCKET}/_pii/output", "ap-south-1"))

    def test_the_per_region_console_host(self):
        u = (f"https://ap-south-1.console.aws.amazon.com/s3/buckets/{self.BUCKET}"
             "?region=ap-south-1&prefix=_pii%2Foutput%2F")
        self.assertEqual(stores.parse_s3(u),
                         (f"s3://{self.BUCKET}/_pii/output", "ap-south-1"))

    def test_a_console_object_url_opens_its_folder(self):
        # Points at one file. The /object/ path segment says so, so backing up
        # to the containing folder is a fact, not a guess.
        u = (f"https://s3.console.aws.amazon.com/s3/object/{self.BUCKET}"
             "?region=ap-south-1&prefix=_pii/output/a/page_1.jsonl")
        self.assertEqual(stores.parse_s3(u),
                         (f"s3://{self.BUCKET}/_pii/output/a", "ap-south-1"))

    def test_the_virtual_hosted_endpoint(self):
        u = f"https://{self.BUCKET}.s3.ap-south-1.amazonaws.com/_pii/output/"
        self.assertEqual(stores.parse_s3(u),
                         (f"s3://{self.BUCKET}/_pii/output", "ap-south-1"))

    def test_the_path_style_endpoint(self):
        u = f"https://s3.ap-south-1.amazonaws.com/{self.BUCKET}/_pii/output/"
        self.assertEqual(stores.parse_s3(u),
                         (f"s3://{self.BUCKET}/_pii/output", "ap-south-1"))

    def test_the_regionless_endpoint_has_no_region(self):
        u = f"https://{self.BUCKET}.s3.amazonaws.com/_pii/output"
        self.assertEqual(stores.parse_s3(u), (f"s3://{self.BUCKET}/_pii/output", None))

    def test_an_arn(self):
        u = f"arn:aws:s3:::{self.BUCKET}/_pii/output"
        self.assertEqual(stores.parse_s3(u), (f"s3://{self.BUCKET}/_pii/output", None))

    def test_a_plain_uri_still_works_and_loses_its_trailing_slash(self):
        # Both spellings must land on ONE string, or one run gets two recents
        # entries and two unrelated sets of verdicts in marks.json.
        a = stores.parse_s3(f"s3://{self.BUCKET}/_pii/output/")
        b = stores.parse_s3(f"s3://{self.BUCKET}/_pii/output")
        self.assertEqual(a, b)
        self.assertEqual(a[0], f"s3://{self.BUCKET}/_pii/output")

    def test_a_bucket_with_dots_is_not_read_as_an_endpoint(self):
        u = "https://my.data.bucket.s3.eu-west-1.amazonaws.com/x/y"
        self.assertEqual(stores.parse_s3(u), ("s3://my.data.bucket/x/y", "eu-west-1"))

    def test_things_that_are_not_s3(self):
        for spec in ("~/Desktop/pii-demo", "/tmp/export.zip",
                     "https://example.com/s3/buckets/x", "", "   "):
            self.assertIsNone(stores.parse_s3(spec), spec)


class RunRoot(unittest.TestCase):
    """A location pasted while looking at the result points at the output half.

    Opened literally that is a review whose left pane is empty on every row,
    which reads as "the pipeline dropped everything" rather than "you pointed
    one level too deep".
    """

    def test_the_pipeline_output_prefix_is_climbed_out_of(self):
        for spec in ("s3://b/run1/_pii/output", "s3://b/run1/_pii/output/",
                     "s3://b/run1/_pii", "/x/run1/_pii/output"):
            self.assertIn(pairing.run_root(spec), ("s3://b/run1", "/x/run1"), spec)

    def test_a_run_root_is_left_alone(self):
        for spec in ("s3://b/run1", "s3://b", "/x/y", "s3://b/output"):
            self.assertEqual(pairing.run_root(spec), spec, spec)

    def test_it_never_climbs_past_the_bucket(self):
        self.assertEqual(pairing.run_root("s3://_pii/output"), "s3://_pii/output")

    def test_a_console_url_is_canonicalised_before_it_is_climbed(self):
        # The URL's depth lives in ?prefix=, not in its path. Climbing first
        # silently did nothing and the review opened on the output half alone.
        u = ("https://s3.console.aws.amazon.com/s3/buckets/bk"
             "?region=ap-south-1&prefix=_pii/output/")
        self.assertEqual(review.resolve_root(u), "s3://bk")

    def test_a_local_folder_is_untouched(self):
        self.assertEqual(review.resolve_root("~/Desktop/pii-demo"), "~/Desktop/pii-demo")


class Viewing(unittest.TestCase):
    """view() must never hand a document to the browser to save.

    Every one of these was a live download dialog: the pane went blank and the
    file landed in ~/Downloads instead, twice per refresh, once per side.
    """

    def test_a_huge_cell_still_renders_as_a_table(self):
        # A Discord/Google takeout row packs a whole JSON blob into one field.
        # csv caps a field at 128 KB and raises past it, which used to drop the
        # file onto the raw byte route.
        blob = "x" * 200_000
        data = f'id,data\n1,"{blob}"\n'.encode()
        out = review.view("users.csv", data)
        self.assertEqual(out["kind"], "table")

    def test_the_csv_field_limit_is_left_as_it_was_found(self):
        import csv
        before = csv.field_size_limit()
        review.view("users.csv", b'a,b\n1,"' + b"x" * 200_000 + b'"\n')
        self.assertEqual(csv.field_size_limit(), before)

    def test_an_unlisted_text_format_is_sniffed_not_downloaded(self):
        out = review.view("app.properties", b"mail.from=a@b.com\nmail.to=c@d.com\n")
        self.assertEqual(out["kind"], "text")
        self.assertIn("a@b.com", out["html"])

    def test_binary_is_not_mistaken_for_text(self):
        self.assertEqual(review.view("blob.bin", b"\x00\x01\x02rubbish")["kind"], "other")

    def test_an_unshowable_document_says_why(self):
        out = review.view("contract.doc", b"\xd0\xcf\x11\xe0" + b"\x00" * 64)
        self.assertEqual(out["kind"], "other")
        self.assertIn("docx", out["why"])

    def test_a_broken_workbook_reports_instead_of_downloading(self):
        out = review.view("book.xlsx", b"not a zip at all")
        self.assertEqual(out["kind"], "other")
        self.assertTrue(out["why"])

    def test_slides_are_read_in_slide_order(self):
        import io as _io
        def slide(text):
            A = "http://schemas.openxmlformats.org/drawingml/2006/main"
            P = "http://schemas.openxmlformats.org/presentationml/2006/main"
            return (f'<p:sld xmlns:p="{P}" xmlns:a="{A}">'
                    f'<a:p><a:t>{text}</a:t></a:p></p:sld>').encode()
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n, t in ((1, "first"), (2, "second"), (10, "tenth")):
                z.writestr(f"ppt/slides/slide{n}.xml", slide(t))
        out = review.view("deck.pptx", buf.getvalue())
        self.assertEqual(out["kind"], "text")
        body = out["html"]
        self.assertLess(body.index("second"), body.index("tenth"))

    def test_the_viewer_never_falls_through_to_an_iframe(self):
        # "other" is a card the page draws. "raw" is the PDF plugin. Those are
        # the only two non-rendered kinds, and view() must not invent a third.
        for name, data in (("a.txt", b"hello"), ("a.csv", b"a,b\n1,2\n"),
                           ("a.png", b"\x89PNG"), ("a.pdf", b"%PDF-1.4"),
                           ("a.weird", b"\xff\xfe\x00\x01")):
            self.assertIn(review.view(name, data)["kind"],
                          {"table", "text", "records", "image", "pdf", "other"}, name)


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
        # Found as "exact" rather than "name": aa/ and zz/ hold one file each
        # and the shape is unique on both sides, so the folders are lined up
        # first and the filenames then match inside them. Stronger evidence
        # than the last-resort join on filename alone, and the same pair.
        idx = self.build({"u/raw/aa/only-one.pdf": b"a", "u/out/zz/only-one.pdf": b"A"},
                         left_only="raw", right_only="out")
        self.assertEqual([(p["left"], p["right"]) for p in idx["pairs"]],
                         [("u/raw/aa/only-one.pdf", "u/out/zz/only-one.pdf")])

    def test_ambiguous_shapes_are_not_lined_up_by_guesswork(self):
        # Two folders a side, all four holding the same one filename. Nothing
        # says which maps to which, and guessing puts one person's source
        # beside another person's output -- every difference then reads as a
        # leak. Better to leave them unpaired.
        idx = self.build({
            "u/raw/aa/page_000001.jsonl": b"a", "u/raw/bb/page_000001.jsonl": b"b",
            "u/out/cc/page_000001.jsonl": b"A", "u/out/dd/page_000001.jsonl": b"B",
        }, left_only="raw", right_only="out")
        self.assertEqual(idx["pairs"], [])

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
