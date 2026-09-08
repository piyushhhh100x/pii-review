# Redaction reviewer

Source on the left, what the pipeline produced on the right. Eyeball a hundred
documents in a sitting without touching the mouse.

## Quick start: access key, secret key, bucket

Three values and one command, on a machine that has never seen this repo. No
AWS CLI, no `aws configure`, no profile file — the app reaches S3 through
boto3, which reads the standard `AWS_*` environment variables.

**macOS / Linux**

```bash
# 1. Code, and a Python that can reach S3.
git clone https://github.com/pritammishra-glitch/pii-review.git
cd pii-review
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install --quiet boto3

# 2. The only lines you edit.
export AWS_ACCESS_KEY_ID='AKIAEXAMPLE000000000'
export AWS_SECRET_ACCESS_KEY='example-secret-key-do-not-use'
export AWS_DEFAULT_REGION='ap-south-1'
BUCKET='example-bucket'
PREFIX='assignments/d/gsuite/qa-0000000/'

# 3. Go.
python3 review.py "s3://$BUCKET/$PREFIX"
```

**Windows PowerShell**

```powershell
git clone https://github.com/pritammishra-glitch/pii-review.git
Set-Location .\pii-review
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --quiet boto3

$env:AWS_ACCESS_KEY_ID     = 'AKIAEXAMPLE000000000'
$env:AWS_SECRET_ACCESS_KEY = 'example-secret-key-do-not-use'
$env:AWS_DEFAULT_REGION    = 'ap-south-1'
$BUCKET = 'example-bucket'
$PREFIX = 'assignments/d/gsuite/qa-0000000/'

python review.py "s3://$BUCKET/$PREFIX"
```

The app prints `http://127.0.0.1:8765/` and opens it. `Ctrl-C` stops it. Give
it a moment before assuming it hung — it lists the whole prefix to build the
index first, and prints nothing while it does.

Set the region to the bucket's own region. A wrong region fails to list even
with valid keys, and a bare bucket name carries no region of its own — only a
console URL does.

### Check access first

If listing is denied, the app exits with a traceback rather than opening. One
command tells you before you start, and needs no AWS CLI:

```bash
python3 -c "import boto3;print(boto3.client('s3').list_objects_v2(Bucket='example-bucket',MaxKeys=1).get('KeyCount'))"
```

A number means you are in. `AccessDenied` on **`s3:ListBucket`** is fatal and
not something this tool can work around — listing is what builds the index, so
`s3:GetObject` alone is not enough. Ask the bucket owner for both, on the
bucket *and* its contents:

```
arn:aws:s3:::example-bucket
arn:aws:s3:::example-bucket/*
```

`aws sts get-caller-identity` names the user you are actually authenticating
as, which is usually the surprise.

### Variations on that one command

| What you have | What to run |
| --- | --- |
| Only a bucket name | `python3 review.py "s3://$BUCKET/"` |
| A console URL from the address bar | `python3 review.py 'https://s3.console...&prefix=p/'` — quote it, or `&` backgrounds your shell |
| Source and output as separate prefixes | `python3 review.py --pair "s3://$BUCKET/export/" "s3://$BUCKET/_pii/output/export/"` |
| A second run open already | add `--port 8766` — one port per run |
| Keys you would rather not retype | put them in a profile (see **S3 setup**) and pass `--profile NAME` |

`--profile` and the `AWS_*` variables are alternatives; you need one, not both.
Passing `--profile` makes the app use that profile and ignore the variables.

### If you would rather not export keys

Environment variables live only in the current shell, which is why they suit a
one-off. For anything you repeat, use a named profile instead — it keeps the
key material out of your shell history entirely. See **S3 setup** below.

## For coding agents

A runbook for Claude Code, Codex and the like: set the app up and confirm it is
serving, in one pass, with no human at the keyboard. Read this whole section
before running anything — three of these steps fail in ways that look like
success.

### The contract

| | |
| --- | --- |
| **Inputs you need** | access key, secret key, bucket region, and a bucket (plus prefix, if any) |
| **Never** | write key material into the repo, a command that gets logged, or `~/.aws/credentials` without being asked. Pass it in the environment |
| **The process** | a long-running HTTP server. Start it in the background and poll; it does not return |
| **Readiness** | it prints `  http://127.0.0.1:PORT/` once indexing is done, then binds. Confirm with `GET /api/boot` returning JSON whose `pairs` array is non-empty |
| **Never pipe its output** | `python3 review.py ... \| tail` reports **`tail`'s** exit status, so a crash looks like success, and the pipe buffers the log so a healthy run looks silent. Redirect to a file instead |
| **Startup is slow and quiet** | nothing is printed until the whole prefix has been listed. On a large bucket that is minutes. An empty log means "still indexing", not "broken" |

### One-shot setup

Run each block, in order, and stop at the first that fails.

```bash
# 1. Environment. Substitute the four values; export nothing else.
export AWS_ACCESS_KEY_ID='...'
export AWS_SECRET_ACCESS_KEY='...'
export AWS_DEFAULT_REGION='ap-south-1'
TARGET='s3://example-bucket/example-prefix/'

# 2. Dependencies. Isolated, so the host Python is untouched.
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install --quiet boto3

# 3. Preflight. Cheaper than a failed launch, and the error is legible.
TARGET="$TARGET" python3 - <<'EOF'
import boto3, os, sys, urllib.parse
b = urllib.parse.urlparse(os.environ["TARGET"])
try:
    r = boto3.client("s3").list_objects_v2(
        Bucket=b.netloc, Prefix=b.path.lstrip("/"), MaxKeys=1)
    print("OK keys>=", r.get("KeyCount"))
except Exception as e:
    sys.exit("PREFLIGHT FAILED: %s" % e)
EOF

# 4. Launch, backgrounded, browser suppressed.
python3 review.py "$TARGET" --port 8765 --no-open > /tmp/review.log 2>&1 &

# 5. Wait for readiness. Poll the API, not the log.
for i in $(seq 1 90); do
  if grep -qE 'Traceback|AccessDenied|Error' /tmp/review.log; then
    tail -20 /tmp/review.log; exit 1
  fi
  n=$(curl -sf http://127.0.0.1:8765/api/boot \
      | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["pairs"]))' 2>/dev/null)
  if [ -n "$n" ] && [ "$n" -gt 0 ]; then echo "ready: $n pairs"; break; fi
  sleep 2
done
```

On Windows PowerShell, the same steps with `$env:NAME = '...'`,
`.\.venv\Scripts\Activate.ps1`, `python` for `python3`, and
`Start-Process`/`Get-NetTCPConnection` in place of `&` and `curl`. Note that
PowerShell 5.1 has no `&&` — chain with `;` and an `if ($?)` guard.

### Rules that save a wasted run

**Always pass `--no-open`.** Without it the app launches a browser on a machine
that may have none.

**One port per run, and check it is free yourself.** The server sets
`allow_reuse_address` (review.py), so a second run on a taken port does not
reliably fail loudly — on Windows especially it may bind alongside the first
and serve confusing results. Never assume a listener on 8765 is yours: it may
be an earlier run of a *different* bucket, indistinguishable until you read
`left`/`right` from `/api/boot`.

**`/api/boot` is the source of truth for what is loaded.** Its `left` and
`right` are the two prefixes actually resolved, and `pairs` is how many
documents were matched up. Confirm these are the ones you were asked for; a
server answering 200 proves only that it started.

**`s3:ListBucket` denial is terminal.** Retrying, reformatting the URL or
changing region will not fix it, because listing is what builds the index.
Report it and stop — the fix is an IAM grant from the bucket owner, and no
amount of tool cleverness substitutes.

**Do not sweep credential profiles.** Trying each entry in `~/.aws/credentials`
to see which one opens a bucket is credential probing, and agent sandboxes
rightly block it. Ask which profile owns the bucket.

**Region is not optional and not guessable.** A bare `s3://bucket/prefix`
carries no region; only a console URL does. Wrong region fails to list with
valid keys.

**Prefer a named profile for anything repeated.** Environment variables die
with the shell, which is what makes them right for a one-shot and wrong for a
workflow you will run again.

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

Every S3 location needs credentials, from either `--profile` or the `AWS_*`
environment variables (see **Quick start**). Which ones depends on who owns the
bucket — a client bucket is usually a different AWS account from your own, and
your SSO login does not reach it. Two ways in.

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

Python 3.9+. Local folders and zips need the standard library only; S3 needs
`boto3` (`pip install boto3`), or failing that the `aws` CLI on PATH. PDFs scroll in step
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
standard library. S3 additionally needs `boto3`, or the `aws` CLI as a fallback.

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

For S3, install `boto3` — it is what the app prefers, and it avoids the CLI
entirely:

```bash
python3 -m pip install boto3
```

The `aws` CLI is only a fallback for when `boto3` cannot be imported, though it
is still the handiest way to *diagnose* access (`aws sts get-caller-identity`):

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

This only matters if `boto3` is missing. Install `boto3` into the interpreter
that runs the app (`python -m pip install boto3`) and the CLI is not needed at
all. If you do want the CLI, put its directory on PATH, restart PowerShell and
verify with `aws --version`; the app invokes `aws.cmd` on Windows.

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
