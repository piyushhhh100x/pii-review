---
name: setup-redaction-reviewer
description: Set up and launch the redaction reviewer on any machine (macOS, Linux, Windows) in one pass — check the interpreter, start the app, and wire up the optional PDF and S3 extras. Use when someone asks to install, set up, bootstrap, or first-run this tool on a new box.
---

# Set up the redaction reviewer

A checklist for an agent working on a machine that has never run this tool.
The app starts on the standard library alone, but it does not display
anything without PyMuPDF — step 4, which is not optional however much its
name suggests it. Step 5 is a genuine extra, for S3 runs only.

Work through it top to bottom. Report at the end which steps applied and
which were skipped, and paste the URL the app printed.

## 1. Know which shell you are in

Every command below is written twice where the two differ. Pick the column
for the machine you are on and stay in it.

| | macOS / Linux | Windows |
|---|---|---|
| interpreter | `python3` | `python` |
| launcher | `./review` | `python review.py` |
| repo path | `~/Desktop/pii-review` | wherever it was cloned |

On Windows the `review` shell script will not run — call `review.py` with the
interpreter directly. Do not add a `.cmd` or `.ps1` wrapper; it is one extra
file to keep in step with the shell script for no gain.

## 2. Check the interpreter

```
python3 --version        # Windows: python --version
```

Needs **3.9 or newer**. If it is missing or older, install a current CPython
(python.org, `brew install python`, or the distro's package) and re-check
before going on. Do not create a virtualenv — the app imports nothing outside
the standard library, so a venv only adds a path to get wrong.

## 3. Start it

From the repo root:

```
cd <repo>
./review                 # Windows: python review.py
```

It prints a URL and opens a browser. A popup asks where the run is; give it
one location — a folder, a zip, or anything naming an S3 location.

Confirm it came up before moving on. If port 8765 is taken, pass
`--port 8790`. To skip the browser, pass `--no-open` and open the printed URL
yourself.

Verify without a real run in hand:

```
python3 review.py --help
```

Options worth knowing: `--pair SOURCE OUTPUT` when the two halves are
siblings rather than nested, `--profile` for S3, `--mappings` to point at a
`pii_mappings.db`, `--seed` to review someone else's sample.

## 4. PyMuPDF — required, despite the name

Do not skip this, whatever the run contains.

PyMuPDF reads PDFs, so it looks like it should only matter for a run that has
them. It does not work out that way here. The browser sets its whole
document-fetching flag from whether the server found PyMuPDF, so on a machine
without it **no document of any type loads** — `.eml`, `.json`, `.csv` and the
rest all sit there saying "could not ask the server about this document"
while the app itself looks fine. Installing PyMuPDF is what turns fetching
back on.

The app shells out to any interpreter on the box that can `import fitz`; it
does not have to be the one running the app, and it remembers the one it
found in `.renderer`.

```
python3 -m pip install --user PyMuPDF
python3 -c "import fitz; print(fitz.__doc__)"     # must succeed
```

Confirm the panes actually fill with a document before calling the setup
done. That check is the point of this step — a clean start proves nothing.

Once installed it also does the job it is named for: the two panes scroll in
step through a PDF instead of falling back to the browser's own viewer.

## 5. Optional — S3 access

Skip unless the location is on S3.

The app reads S3 through `boto3` if it is importable, and otherwise shells
out to the `aws` CLI. Either is fine; one of them must be there.

```
python3 -m pip install --user boto3
# or install the AWS CLI v2 and confirm:
aws --version
```

Every S3 location needs `--profile`. Confirm the profile reaches the bucket
*before* handing the location to the app — the error is much clearer here:

```
aws sts get-caller-identity --profile NAME    # which account am I?
aws s3 ls s3://bucket/prefix/ --profile NAME  # can I list it?
```

Listing is what builds the index, so `s3:GetObject` alone is not enough — the
bucket must also allow `s3:ListBucket`. `AccessDenied` on either is a
permissions problem in the account that owns the bucket, and nothing about
this tool can work around it. See the README's "S3: getting in" for setting
up an SSO profile or a keys profile.

## 6. What the app writes

Tell the user about these; none are checked in.

- `marks.json` in the repo — verdicts and comments, keyed by the pair of
  locations. Deleting it discards the review.
- `recent.json`, `.renderer` in the repo — recent locations, and the
  interpreter found in step 4.
- `~/.pii-review-salt` — written once per machine, seeds which files land in
  this reviewer's sample. **Do not copy it between machines or delete it** —
  it is what keeps two reviewers on different hundreds, and what keeps one
  reviewer's hundred stable across refreshes and re-clones.

## Troubleshooting

- **`./review` says permission denied** — `chmod +x review`.
- **Browser opens on a blank page** — the port was taken by another tab of
  this same app. Ctrl-C every instance, or start with `--port`.
- **Right pane identical to the left, or blank** — not a setup problem. That
  is the finding: the redaction never ran, or it broke.
- **PII-mappings panel asks for a database** — the run shipped no
  `_pii/pii_mappings.db`. Point at one with `--mappings`, or leave it; the
  app falls back to diffing the pair.
- **Both panes say "could not ask the server about this document", and no
  file of any type opens** — PyMuPDF is missing. This is step 4, and it is
  not the PDF-only problem it sounds like; see that step.
- **Test suite** — `python3 -m unittest test_review -q`. On Windows a couple
  of tests fail on `\` vs `/` in path assertions; that is the suite, not the
  install.
