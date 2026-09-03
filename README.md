# Redaction reviewer

Source on the left, what the pipeline produced on the right. Eyeball a hundred
documents in a sitting without touching the mouse.

## Run it

```
cd ~/Desktop/pii-review
./review
```

A popup asks where the run is. Give it **one** location — a folder, a zip, or
anything that names an S3 location — and it works out which half is the source
and which is the output. Ctrl-C to stop.

For S3, paste whatever you have. The console URL out of the address bar, either
REST endpoint, an ARN, or the `s3://` URI all work, and the region comes off the
URL when it is there. Pasting a location inside `_pii/output/` opens the run
that output belongs to, so both halves are there to compare.

Skip the popup:

```
./review ~/Downloads/some-export
./review s3://bucket/export --profile sail
./review 'https://s3.console.aws.amazon.com/s3/buckets/bkt?region=ap-south-1&prefix=_pii/output/' --profile sail
```

Quote a console URL — the `&` will otherwise background your shell.

## Several at once

Give more than one location, or repeat `--pair`, and each opens in its own
tab on its own port. Verdicts are keyed by the pair of locations, so the tabs
never overwrite each other.

```
./review ~/Downloads/run-a ~/Downloads/run-b ~/Downloads/run-c

./review --profile sail \
  --pair s3://bkt/export s3://bkt/export-pii \
  --pair s3://bkt/export/unit-a s3://bkt/export-pii/unit-a
```

Use `--pair` when the two halves are siblings rather than nested. Ctrl-C stops
all of them.

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
