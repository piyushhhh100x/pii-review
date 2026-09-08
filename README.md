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

## S3: getting in

Every S3 location needs `--profile`. Which one depends on who owns the bucket —
a client bucket is usually a different AWS account from your own, and your SSO
login does not reach it. Two ways in.

**SSO, for accounts on your own start URL:**

```
aws sso login --profile sail
./review s3://bucket/prefix --profile sail
```

To see which accounts that login actually covers — buckets outside them will
fail no matter how many times you re-login:

```
aws sso list-accounts --region us-west-2 --access-token \
  "$(jq -r 'select(.accessToken)|.accessToken' ~/.aws/sso/cache/*.json | head -1)"
```

Add a profile per account to `~/.aws/config`:

```ini
[profile some-client]
sso_session    = sail
sso_account_id = 111122223333
sso_role_name  = SomeRole
region         = ap-south-1
```

**Access keys, for a client account you were handed credentials for.**
Put them in `~/.aws/credentials` — never in a command, a script, or this repo:

```ini
[some-client]
aws_access_key_id     = AKIA...
aws_secret_access_key = ...
```

```
chmod 600 ~/.aws/credentials
aws sts get-caller-identity --profile some-client    # confirms which account
./review s3://client-bucket/their-export --profile some-client
```

**If it will not open**, run the listing by hand — the error names the missing
permission, and `AccessDenied` on `s3:ListBucket` is not something this tool
can work around:

```
aws s3 ls s3://bucket/prefix/ --profile some-client
```

Listing is what builds the index, so read access alone is not enough. Ask for
`s3:ListBucket` and `s3:GetObject` on that bucket, or for credentials in the
account that owns it.

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

The search box covers **every** pair in the run, not just your share. Type a
path a teammate reported and it opens, whether or not it was dealt to you;
hits from outside your sample are tinted and review exactly like the rest.

Press `n`, or **Folder-names**, for what the pipeline did to the folder names
themselves. A folder is part of the deliverable too:
`gmail/anirudh.trivedi@inc42.com/messages/page_000001.jsonl` names a person and
their employer in the object key however clean the two panes look, and neither
pane shows it. The panel is one table — every folder, its name in the source, its name in the
output — searchable. A name the output kept that reads as personal data is
tinted. The folder each document sits in is also printed above its pane,
source against output.

Two people on the same run get different files. The sample is seeded on a salt
kept in `~/.pii-review-salt`, written once per machine — so your files stay the
same across refreshes and re-clones, and your colleague's hundred is a
different hundred. On one real export two reviewers covered 631 files between
them instead of 340.

To review exactly what someone else is reviewing, pass their sample id (it is
printed on startup and shown in the header):

```
./review --pair SRC OUT --profile sail --seed 256028
```

Press `m`, or the **PII-mappings** button, for the run's substitution table:
every attribute type down the side with its real count, the exact rows in the
middle, paged. Nothing is sampled away — every original the pipeline found,
what it replaced it with, and the ones it found and left alone. Search spans
every type, and you can leave a comment on any row. It is picked up from
`_pii/pii_mappings.db` beside the output if the run shipped one, otherwise
point at it — either on the command line or in the panel itself, which asks
when the run shipped none:

```
./review --pair SRC OUT --profile sail --mappings ~/runs/_pii/pii_mappings.db
./review --pair SRC OUT --profile sail --mappings s3://bucket/run/_pii/pii_mappings.db
```

Verdicts save to `marks.json` as you go, keyed by the pair of locations, so
closing the tab and coming back tomorrow resumes where you stopped.

Pull out everything you commented on:

```
python3 -c "import json;m=json.load(open('marks.json'));\
[print(k,'|',c) for s,v in m.items() for k,d in v.items() for c in d.get('comments',[])]"
```

## Setup from a fresh checkout

These steps are suitable for a new machine. The application itself uses Python's
standard library. AWS CLI is only needed when opening S3 locations.

### 1. Clone the repository

macOS/Linux:

```bash
git clone https://github.com/piyushhhh100x/pii-review.git
cd pii-review
```

Windows PowerShell:

```powershell
git clone https://github.com/piyushhhh100x/pii-review.git
Set-Location .\pii-review
```

### 2. Check Python

Python 3.9 or newer is required.

```bash
python3 --version
```

On Windows, use either `py --version` or `python --version`. If Python is not
installed, install it from <https://www.python.org/downloads/> and enable the
option to add Python to PATH.

### 3. Start the app

macOS/Linux:

```bash
python3 review.py
```

Windows PowerShell:

```powershell
py .\review.py
```

The app prints a local URL such as `http://127.0.0.1:8765/` and normally opens
it in the default browser. Open that URL manually if the browser does not open.
Use `Ctrl-C` in the terminal to stop the server.

### 4. Optional PDF rendering

PDFs work with the browser's built-in viewer without extra packages. For
scroll-synchronised PDF page images and extracted PDF text, install PyMuPDF in
the Python environment used to run the app:

```bash
python3 -m pip install PyMuPDF
```

Windows PowerShell:

```powershell
py -m pip install PyMuPDF
```

Text, JSON, JSONL, CSV, email, XML, DOCX, XLSX, and image files do not require
PyMuPDF.

## S3 setup

Install the AWS CLI only when you need S3:

- AWS CLI installation guide: <https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html>
- Verify it with `aws --version`.

Configure a profile using SSO (recommended):

```bash
aws configure sso --profile example-profile
aws sso login --profile example-profile
aws sts get-caller-identity --profile example-profile
```

Then open a bucket:

```bash
./review s3://example-bucket/example-prefix/ --profile example-profile
```

On Windows PowerShell:

```powershell
py .\review.py "s3://example-bucket/example-prefix/" --profile example-profile
```

For access keys supplied by an administrator, store them in the AWS credentials
file, never in this README, a command line, source code, or a commit:

```ini
[example-profile]
aws_access_key_id = AKIAEXAMPLE000000000
aws_secret_access_key = example-secret-key-do-not-use
region = ap-south-1
```

The credentials file is normally `~/.aws/credentials` on macOS/Linux and
`%USERPROFILE%\.aws\credentials` on Windows. Confirm the account before opening
data:

```bash
aws sts get-caller-identity --profile example-profile
aws s3 ls s3://example-bucket/example-prefix/ --profile example-profile
```

The profile needs `s3:ListBucket` and `s3:GetObject`. Listing is required to
build the review index. If either command returns `AccessDenied`, ask the bucket
owner for access; do not work around the policy or copy credentials into the
repository.

Console URLs are accepted directly. Quote them because `&` has special meaning
in shells:

```bash
./review 'https://s3.console.aws.amazon.com/s3/buckets/example-bucket?region=ap-south-1&prefix=example-prefix/' --profile example-profile
```

### S3 source and redacted pair

When the source and output are separate prefixes, use `--pair`:

```bash
./review --pair \
  s3://example-bucket/source-prefix/ \
  s3://example-bucket/redacted-prefix/ \
  --profile example-profile
```

Windows PowerShell uses the same command on one line, or PowerShell's backtick
for continuation:

```powershell
py .\review.py --pair `
  "s3://example-bucket/source-prefix/" `
  "s3://example-bucket/redacted-prefix/" `
  --profile example-profile
```

Use a different port when another reviewer tab is already running:

```bash
./review --pair SRC OUT --profile example-profile --port 8766
```

## PII highlighting

If a run includes `pii_mappings.db`, the app highlights original values in the
source pane, replacements in the output pane, and detected-but-unreplaced values
as leaks. The mappings panel is available from the **PII-mappings** button.

Some verification samples do not include a mappings database. In that case the
app still highlights common PII patterns such as email addresses, formatted
phone numbers, and SSNs as a visual QA aid. This fallback is not a replacement
for the pipeline's authoritative mappings or a security scanner.

If documents load but no values are highlighted:

1. Confirm the app is running the latest checkout.
2. Refresh the browser tab.
3. Open the **PII-mappings** panel and check whether a mappings database was
   found.
4. Remember that a sample without mappings uses the common-pattern fallback,
   so custom PII formats may require manual review.

## Troubleshooting

**The browser page is blank or says it cannot ask the server about a document.**

Restart the app from the repository directory and open the URL printed by the
new process. Check that the terminal stays running and that the chosen AWS
profile can read the object directly:

```bash
aws s3 cp s3://example-bucket/example-prefix/example.json - \
  --profile example-profile
```

**The app lists S3 folders but cannot read files.**

The profile may have `s3:ListBucket` without `s3:GetObject`, or the object may
be encrypted with a KMS key that the role cannot use. Ask the bucket owner for
both permissions.

**Windows reports that `aws` cannot be found.**

Install the AWS CLI and ensure its installation directory is on PATH. Then
restart PowerShell and verify with `aws --version`. The Windows launcher in
this repository supports the standard `aws.cmd` executable.

**A PDF is downloadable but not rendered as images.**

Install PyMuPDF in the same Python environment used to launch `review.py`, or
use the browser PDF fallback. Other supported document formats do not depend on
PyMuPDF.

**The wrong files are shown.**

Use explicit `--pair SOURCE OUTPUT` paths. For a nested `_pii/output/` URL, the
app attempts to locate the corresponding source run automatically; explicit
paths are preferred when a run has multiple possible source prefixes.

## Security notes

- Never paste real access keys or secret keys into source files, README files,
  issue trackers, chat, shell history, or commits.
- Rotate any credential that was accidentally exposed.
- Prefer short-lived SSO credentials or a narrowly scoped read-only role.
- Review only the minimum S3 prefix required for the QA task.
- `marks.json`, `map_notes.json`, `.renderer`, and local AWS configuration may
  contain local session state; do not commit credentials or sensitive review
  data.
