# Redaction reviewer

Source on the left, what the pipeline produced on the right. Eyeball a hundred
documents in a sitting without touching the mouse.

## Run it

```
cd ~/Desktop/pii-review
./review
```

A popup asks where the run is. Give it **one** location — the folder, zip or
`s3://` prefix holding the whole run — and it works out which half is the
source and which is the output. Ctrl-C to stop.

Skip the popup:

```
./review ~/Downloads/some-export
./review s3://bucket/export --profile sail
```

Python 3.9+, standard library only. Nothing is installed. PDFs scroll in step
if PyMuPDF is importable by any interpreter on the box; without it they fall
back to the browser's viewer.

## Use it

`j` / `k` move between files, `→` / `←` between folders. Press `r` when a
document is clean, `c` to leave a comment, `f`-worthy findings go in the
comment. `?` lists every key in the app.

Anything on the right still showing a person's name, address, DOB, SSN or
personal mobile is a leak. So is the opposite: a toll-free number, a column
heading or a product name that got scrubbed. A right pane identical to the
left means the redaction never ran; a blank one means it broke.

Verdicts save to `marks.json` as you go, keyed by the pair of locations, so
closing the tab and coming back tomorrow resumes where you stopped.

Pull out everything you commented on:

```
python3 -c "import json;m=json.load(open('marks.json'));\
[print(k,'|',c) for s,v in m.items() for k,d in v.items() for c in d.get('comments',[])]"
```
