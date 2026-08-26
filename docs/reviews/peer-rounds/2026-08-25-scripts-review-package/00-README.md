# Review request — two operational scripts, and a Kometa correction

**Branches:** `ops/deploy-and-permission-scripts`, `fix/kometa-dv-badge-mirror`
**Not part of the round 26–29 attestation sequence.** These are separate.

---

## Why this is worth your time

Both scripts have already produced real defects **in production**, on Jesse's
machine, tonight. Neither was caught by reading the code.

| script | what it did | how it was found |
|---|---|---|
| `grant-claude-merge-permission.ps1` | wrote a **UTF-8 BOM** into `~/.claude/settings.json`, making it strictly-invalid JSON | parsing the file afterwards |
| `merge-and-deploy-2026-08-25.ps1` | reported a **0% log baseline** for an hour holding 1,836 lines, 99% of them spam | running `-WhatIf` |

The BOM one is the serious one. The rule it added landed correctly, so the
script "worked" by its own reckoning — but `settings.json` went from `{` to
`\xef\xbb\xbf{` and stopped being valid JSON, which risks breaking **every**
setting in the file rather than the one being added. Project memory already
recorded that PowerShell 5.1's `-Encoding utf8` means "with BOM" here. It was
written that way anyway, and the output was not checked.

The baseline one is subtler and arguably worse: a zero baseline makes the
post-deploy comparison meaningless **in the safe-looking direction**. "Spam went
from 0 to 0" reads as success.

Both are now fixed and both fixes carry a verification step rather than a
promise. That is exactly what I would like attacked.

---

## The thing I most want you to look at

**Roughly two-thirds of `merge-and-deploy-2026-08-25.ps1` has never executed.**

```
line  97  1. Pre-flight        EXECUTED   (found 3 bugs)
line 151  2. Pull requests     EXECUTED   (correctly refused a bad PR)
line 225  3. Deploy            NEVER RUN
line 254  4. Verify            NEVER RUN
line 312  5. Result            NEVER RUN
                               242 non-comment lines total
```

The untested two-thirds is the part that rebuilds a production container and
then decides whether the rebuild was good. I found three bugs in the third that
did run. I have no reason to think the density is lower in the rest.

**The verification block can only be wrong in one direction that matters.** If it
reports success for a broken deploy, the operator walks away. Specific worries:

- `docker port` is matched with `-match '127\.0\.0\.1:9721'` — a substring test.
  Would a partially-bound or differently-shaped output pass it?
- `$LASTEXITCODE` after `docker compose up --build` is **deliberately ignored**,
  because PowerShell 5.1 reports failure for a successful build (docker writes
  progress to stderr). So a genuinely failed build is detected only by the
  container checks that follow. Is that sufficient, or is there a failure mode
  where the build fails and the old container keeps running and passes every
  check?
- The log-rate check waits 3 minutes and thresholds at `> 20` lines. Both numbers
  are guesses.

**`-Revoke` has never run either.** It is the undo for a standing authorization
change; if it is broken, that is discovered at the moment someone wants it.

---

## What actually happened tonight, for context

The scripts were not used for the deploy. I ran the underlying commands by hand
precisely because the script's deploy path was untested — which is the
recommendation I would give anyone else and the reason this package exists.

The deploy itself went fine and is verified:

```
log spam    30.0/min -> 0.3/min      (~43,000 lines/day eliminated)
DV port     127.0.0.1:9721 intact    (this vanished silently on 2026-08-11)
DV key      SET intact
dv_scan     12,773 rows unchanged
errors      0 since restart
```

So the scripts are not urgently needed. That is the good time to review them.

---

## The Kometa correction (`fix/kometa-dv-badge-mirror`, PR #98)

Included because it is the one place tonight where my **judgement** was wrong
rather than my code, and CI caught it rather than me.

I found that `docs/kometa/dv_badges.yml` described a design nothing deployed had
ever used — text overlays anchored top-LEFT, where Kometa actually loads
`/config/dv-layer.yml` with image overlays anchored top-RIGHT. That divergence
had already caused a shipped defect: a developer trusted the file, placed the
version-count badges top-right to *clear* the DV badge, and put them at exactly
the DV badge's coordinates.

**My first fix was wrong.** I rewrote the file to mirror the deployed one — which
destroyed the intended design and broke
`test_metadata_scan_runbook.py::test_kometa_badges_cover_the_closed_managed_label_set`,
a test that exists to keep that file covering the managed label set. I missed it
because I ran only the two files I had touched.

The correction restores all seven overlays and instead fixes what was actually
wrong: the file never said it was a design. See `01-request.md` §4.

---

## Contents

| File | What it is |
|---|---|
| `00-README.md` | this |
| `01-request.md` | the specific attacks I want, and where I think each is weakest |
| `02-scripts.patch` | both scripts, full |
| `03-kometa.patch` | the #98 correction |
