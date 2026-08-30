# Redaction reviewer

Source document on the left, what the pipeline produced on the right. Built to
eyeball a hundred documents in a sitting without touching the mouse.

```
cd ~/Desktop/pii-review
./review
```

A small popup asks where the run is. Give it **one location** — the folder,
zip or bucket holding the whole run — and it works out which part is the
source and which is the output. Ctrl-C to stop.

Skip the popup:

```
./review ~/Downloads/some-export
./review s3://bucket/export --profile myprofile
```

Nothing is installed and nothing is written except your verdicts.
Python 3.9+, standard library only.

## Where a run can live

| | |
|---|---|
| a folder | `~/Downloads/some-export` — or press **Choose…** for the native picker |
| a zip | `~/Desktop/redacted-2026-08-28.zip` — read in place, never extracted |
| S3 | `s3://bucket/export` |

S3 uses `boto3` when it is importable and otherwise the `aws` CLI, so it runs
under a bare system Python. Set a profile in the popup or pass `--profile`.
Objects are fetched one at a time, cached, and the next one is prefetched
while you are looking at the current one.

## Finding the two halves

The popup shows what it found and lets you change it before opening:

```
Source [ raw/ (1325) ▾ ]     Output [ files/ (993) ▾ ]
```

It handles the two shapes runs actually come in:

- **side by side** — `<unit>/raw/…` next to `<unit>/files/…`
- **in place** — the tree is the source and `_pii/output/…` is the result.
  Source shows as *everything else*.

It shows the file count behind each choice because the guess is regularly
wrong in a way only you can see. One download here carries `raw/` (every
document), `files/` (rewritten in place) and `redacted/` (populated for one
customer in thirty) — name alone cannot rank those, so it warns instead of
picking silently.

If the two halves genuinely live apart, click **two separate locations**.

## Reviewing

Two states, nothing else to decide:

- **viewed** — set for you the moment a document is on screen
- **reviewed** — you press `r`, or click **Review** in the top bar

Comments are separate and optional. Type in the box next to the button and
press Enter; add as many as you like. Each appears as a chip under the header
with an × to remove it. A comment does not imply a verdict.

The counter in the top bar reads `viewed · reviewed · files · comments`.

## Keys

| | |
|---|---|
| `j` · `↓` · `space` | next file |
| `k` · `↑` | previous file |
| `→` / `←` | next / previous folder |
| `]` / `[` | both panes forward / back one page |
| `r` | reviewed on or off |
| `c` | add a comment |
| `a` | all files / unreviewed only |
| `s` | show or hide the list |
| `y` | scroll sync on or off |
| `/` | search |
| `e` | change what is being compared |
| `?` | this list, in the app |

Files move on the vertical axis, folders on the horizontal, so neither
surprises the other. Holding a key steps the header immediately and loads only
the document you stop on.

## The list

One row per folder — the user, app or customer the documents belong to — with
`reviewed/total` and a missing count, and a tick once the folder is done.
Clicking a folder opens it; clicking it again closes it. Files inside carry a
status dot (grey seen, green reviewed, amber missing) and a type icon.

The search box matches **both** folder names and file paths, and opens the
folders holding the hits so you can see them without a second click.

## Scroll sync

The panes scroll together, mirrored proportionally so documents of different
lengths stay aligned. `y` turns it off.

Getting there needed the PDFs off the browser's built-in viewer: that viewer is
a plugin document whose scroll position cannot be read or written from the page
around it, so two PDF iframes can never be kept in step. Pages are rendered to
images server-side with PyMuPDF and shown as ordinary scrollable DOM, which
makes sync a one-line mirror of `scrollTop`.

PyMuPDF is usually not installed for the interpreter running this tool, so a
warm worker is started under whichever one does have it — found once and
remembered in `.renderer`. About 50 ms a page, cached. Set `PII_REVIEW_PYTHON`
to name the interpreter yourself. Without PyMuPDF the panes fall back to the
browser's PDF viewer and only pixel sync is lost.

## The badge next to each filename

An exact-name join finds only the files the pipeline left alone, because the
output's filenames are themselves scrubbed —
`610044998-Experian-CreditReport-CRVD-…` ships as
`610044998-Vanova Ventures Corp.-CreditReport-CRVD-…`. So within a folder the
match cascades, and the badge says how each pair was made:

| badge | meaning |
|---|---|
| `exact` | same filename both sides |
| `fuzzy` | filename partly rewritten, matched on the tokens that survived |
| `name` | filename unique on both sides, folders did not correspond |
| `sole` | the only unpaired file left in that folder — nothing else it could be |
| `size` | inferred from size order — **amber. this one is a guess, look twice** |
| `missing` | nothing in the output. Withheld, or never processed — worth knowing which |

Paths with a leading underscore on any segment are pipeline bookkeeping and are
skipped (`_pii/`, `_profile/`, `_state/`, and the `_name` by-products the VLM
writes beside a redacted image). Selecting one as a side still works — the
filter runs below the selector, not above it.

## What to look for

- Anything on the right still showing a **person's** name, address, DOB, SSN,
  or personal mobile → `f`
- **Over-scrub is equally a defect.** An institution's toll-free line, the
  document's own column headings, a product name (one file here ships
  `SOLIDWORKS VERSION` as `HYDRAOVA STUDIOS LLC VERSION`), a creditor's name
  the record needs to stay readable
- The right pane **identical** to the left — the redaction did not apply → `f`
- The right pane **blank or broken** where the left has content → `f`

## Pulling out what you commented on

```
python3 -c "import json;m=json.load(open('marks.json'));\
[print(k,'|',c) for s,v in m.items() for k,d in v.items() for c in d.get('comments',[])]"
```

## Files

| | |
|---|---|
| `review` | launcher |
| `review.py` | server, popup and viewer |
| `render.py` | PDF page rendering, so the panes can scroll together |
| `pairing.py` | layout detection and the match cascade |
| `stores.py` | folder / zip / S3 backends |
| `marks.json` | viewed / reviewed / comments (created on first use) |
| `recent.json` | recent locations, offered in the popup |
