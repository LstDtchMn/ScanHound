# Peer round: the label sync's freshness claim, and a host permission outage

**Branch:** `fix/dv-backwrite-freshness`
**Date:** 2026-08-15
**Reviewer ask:** two independent items. One is a code change with tests; the
other is an operational repair with no diff, included because it changed
production and its root cause is only INFERRED.

---

## Item 1 (code) — `observed=False` on the rating_key back-write

### The defect

`_maybe_dv_auto_sync` gates the scheduled DV label sync on
`get_latest_dv_scan_at(source='scan')` — `MAX(last_seen_at)` — and records the
**pre-sync** value as its watermark (deliberately, so a failed sync retries;
pinned by `tests/test_dv_autosync_watermark.py`).

But `sync_labels` back-writes every matched movie through `upsert_dv_scan`,
whose `ON CONFLICT` set `last_seen_at = CURRENT_TIMESTAMP` unconditionally. The
sync therefore wrote the metric its own gate reads, past the watermark it had
just recorded. One real detection re-armed the trigger permanently.

**Live evidence (production logs, 14 h):** 11 full-library syncs — 20:01, 21:05,
22:56, 00:00, 01:04, 02:08, 03:12, 04:17, 05:22, 06:25, 07:30 — against a
detector that runs every 6 h. Three added zero labels (684→684, 718→718). The
guard's own comment calls firing it regardless "pure waste".

Second effect, same write: it passes no signature, and the upsert took the
incoming NULLs, blanking `sig_mtime`/`sig_size` on every matched row.
Taking a caller's NULLs is CORRECT for a failed host scan (it forces a retry —
the docstring says so); it is wrong for an annotation that opened no file.
Live: 57 rating_key rows carried NULL signatures.

### The change

`upsert_dv_scan(..., observed=True)`. `observed=False` preserves `last_seen_at`
and the sig columns via `CASE WHEN ? = 1` (matching the existing retraction-SQL
idiom in this file) while still applying everything the caller supplies. Exactly
one caller passes False: the back-write, which annotates from Plex.

`source` is deliberately NOT preserved here — the seed→scan question is a
separate finding (below), and widening this change to cover it would mix two
fixes.

### Tests — and why they are not vacuous

`tests/test_dv_backwrite_does_not_claim_freshness.py`, 4 tests. Every one pins
`last_seen_at` to a fixed past value FIRST: `CURRENT_TIMESTAMP` has one-second
granularity, so a broken build writing "now" twice inside one second is
indistinguishable from a correct one. Wall-clock movement would have made the
suite pass on the bug roughly at random.

Both controls are present, because each catches a different way to "fix" this
wrongly:
* `test_observed_write_still_refreshes_positive_control` — an upsert that never
  updates `last_seen_at` would satisfy the preservation tests and break the
  detector's freshness entirely.
* the second half of `test_the_gate_itself_does_not_advance` — the gate MUST
  still advance on a genuine detection, or DV labels stop reaching Plex.
* the preservation tests also assert `rating_key == "42"`, so a statement that
  silently did nothing cannot pass as "preserved".

**Mutation-verified, by line (not by string — the anchors were checked unique
first):**
* `ELSE dv_scan.last_seen_at END` → `ELSE CURRENT_TIMESTAMP END` (the original
  bug) → fails `test_unobserved_write_preserves_last_seen_at` AND
  `test_the_gate_itself_does_not_advance`.
* `ELSE dv_scan.sig_mtime END` → `ELSE excluded.sig_mtime END` → fails
  `test_unobserved_write_preserves_the_signature` (`None == 1000.0`).
* Restored → 4 passed.

Targeted suite green: 62 passed across `test_dv_backwrite_does_not_claim_freshness`,
`test_dv_labeler`, `test_dv_scan_db`, `test_dv_autosync_watermark`. The existing
exact-call assertion at `test_dv_labeler.py:156` was updated to include the new
kwarg — intentionally, that assertion is the pin.

**Whole-tree run NOT done.** The suite ran from `git archive` (tracked files
only, i.e. what CI checks out) because a full `docker cp` of the working tree
stalls on `data/browser-profiles`. Past experience here is that only whole-tree
runs catch cross-module breakage, so treat this as a gap.

### What I want challenged

1. Is preserving `last_seen_at` on the back-write right, or should the WATERMARK
   move to the post-sync value instead? I chose the former because
   `last_seen_at` means "a scanner saw this file" and the labeler is not a
   scanner — but the alternative is a one-line change in `app_service` and would
   not touch shared DB semantics.
2. `observed` defaults to True, so every other caller is unchanged. Is a
   defaulted boolean the right shape, or should the back-write use a dedicated
   `annotate_rating_key()` that cannot touch layer/source at all?
3. Does any consumer legitimately depend on the back-write refreshing
   `last_seen_at`? I found none, but this is the assertion I am least sure of.

---

## Item 2 (operational, no diff) — every file in `C:\Tools` had an empty DACL

DV detection was dead ~12 h. `dovi_tool`, `ffmpeg`, `ffprobe`, `ffplay`,
`MediaInfo` and the DLLs each had `D:PAI` with **zero ACEs**; the DIRECTORY was
intact (SYSTEM:F, Administrators:F, Users:RX).

The symptom pointed at the wrong layer: the detector recorded
`[WinError 5] Access is denied` against ~3,900 MEDIA files. Every media-file
hypothesis tested clean — `os.stat`, `open()+read()`, and the media file's own
ACL were identical to files that succeeded. The denial was on **CreateProcess**,
not on opening the input; the giveaway is that the error text names no file,
whereas `open()`'s PermissionError includes the path. `dovi_tool --version`
(no file at all) reproduces it in one step.

Repaired with `icacls <file> /inheritance:e` per file, letting the healthy
parent supply the ACEs (NLSur owns them, so WRITE_DAC is implicit — no
elevation). Verified on the FILES, then functionally: dovi_tool 2.3.1 runs, and
a previously-"denied" title extracted an RPU in 1 s reporting **Profile 7 (FEL)**.
3,769 rows whose only error was this outage were reset (`attempts=0`,
`next_retry_at=NULL`, `last_error=NULL`); `is_retry_due(NULL)` is True by
design, so they are eligible immediately. 131 rows keep a genuine
`No HEVC video track` error and were left alone.

**Root cause is INFERRED, not proven.** The dir-healthy/files-empty asymmetry is
the signature of `icacls <dir> /grant X:(OI)(CI)F /T` — inheritance flags are
container-only, so on a FILE they yield no effective ACE. This is the second
occurrence of that damage here. I did not establish WHICH command ran or when;
the first denial timestamp is 2026-08-15 00:29:17. **If the reviewer can suggest
a way to attribute it, that is worth more than the repair** — an unattributed
recurrence will happen a third time.

Also note a methodological error worth flagging: my first "reproduction" ran
dovi_tool from my own session and failed identically — but that session could
not launch it either, so both arms failed for the same reason and the test
distinguished nothing. I reported a root cause one message before it was
actually established.

---

## Findings NOT fixed here (from the same hunt — deliberately out of scope)

Confirmed by adversarial verification, left for separate changes:

* **Partial `dv_label_vocab` strips labels.** `_vocab_from_config` accepts a
  PARTIAL vocab (falls back only when the parse is empty). A layer missing from
  it yields `desired=None` while still authoritative → `may_remove=True` in
  EVERY mode including the hourly additive-only sync → the correct badge is
  removed with nothing added back. Armed by editing the setting the planned
  FEL/DV7/DV8/DV5 tag set MUST edit. **This is the next fix.**
* **Seed→scan laundering.** `upsert_dv_scan` preserves `dv_layer` when the
  incoming is 'unknown' but sets `source = excluded.source` unconditionally, so
  one failed host detection converts a `source='seed'` row to `'scan'`, making
  seed evidence removal-authorizing.
* **No staleness/signature gate in the removal path.** `mediainfo._cached_dv_layer`
  guards on `dv_scan_is_current`; the labeler has no equivalent, so a row for a
  since-replaced file still authorizes removal.
* **`pick_layer` rule 2 vs. multi-copy titles.** Any Plex part lacking a row
  pins the title to `unknown` forever — blocking stale-label removal AND the
  planned HDR10-only tag. This is why the scan-root exclusion was reversed
  today (see `dv-scan-root-expansion.md`).

Refuted on verification, recorded so they are not re-raised: the claimed
container-path/Plex-path mismatch for tonight's rows (the host detector records
host spellings, and the live roots match Plex's forms); the destructive-reconcile
removal semantics (documented, test-pinned, opt-in); and the "adding DV7 strips
it" claim (MANAGED is a closed set, so an unlisted label never enters the
removal set — which is itself the constraint the tag set must respect).

The hunt's own report-writer died on a session limit; findings were read from
the run journal, and the two with live consequences were re-verified by me
against production data. One agent claim was WRONG on the facts: duplicate-path
collisions exist (21) but ZERO disagree on layer, so the label-flapping bug is
LATENT, not live as claimed.
