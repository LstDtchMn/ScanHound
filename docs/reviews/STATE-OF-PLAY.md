# ScanHound — state of play

**Last updated:** 2026-08-10 by Claude (session `e7d059a1`, since archived)

This file exists on `main` so that anyone starting from `main` can see what is outstanding
without having to know which branch to look at. **Read this before starting work on DV
detection, the Turnstile/download-link path, or the queue-recovery policy tests.**

---

## 1. The one thing to know before touching anything

**`main` does not contain the current DV work, and its `backend/rename/dv_detect.py` parser has
a defect that can remove a managed Plex label.**

`_classify()` on `main` tests `"FEL" in sub` as a raw substring *before* looking at the profile,
and returns an authoritative `MEL` for a tokenless `Profile: 7`. So:

| summary | `main` returns | correct |
|---|---|---|
| `Profile: 8 (FEL)` | `fel` | `profile8` |
| `Profile: 7 (NOT FEL)` | `fel` | `unknown` |
| `Profile: 7` | `mel` (authoritative) | `unknown` |

`mel` is a **managed** label: `may_remove = authoritative or not additive_only`, and
`is_authoritative('mel')` is True, so an ambiguous parse can **replace** a real badge. Separately,
`_parse_info()` starts at `LAYER_NONE`, so an unreadable `dovi_tool info -s` — reached only *after*
a non-empty RPU has already proven Dolby Vision exists — returns an authoritative "no Dolby
Vision".

All of this is fixed on `agent/dv-detector-consolidation`. **Do not build on `main`'s parser.**

## 2. Where the work is

| Branch | Head | What it is |
|---|---|---|
| `agent/dv-detector-consolidation` | `e196de4` | DV detector + wrapper + imports, consolidated from two parallel sessions. **4 review rounds, round 4 outstanding.** Suite 4689 passed / 5 skipped |
| `agent/dv-gate-evidence-for-consolidation` | `9c63de1` | The gate evidence and rollback pre-image, branched off the consolidation. Docs/data only, 0 conflicts — **merge it alongside so the evidence travels with the code it justifies** |
| `agent/turnstile-consolidation` | `49625ff` | The Turnstile / download-link fold — **instructions only, nothing folded yet** |
| `agent/hdr10plus-design-review` | `8fbac87` | The live-progress wrapper, approved over 3 rounds and verified on a real scheduled run. **This is what the `ScanHound-DVScan` task currently executes** |

Retire when the consolidation lands: `fix/dv-import-cadence`, `fix/dv-scan-live-progress`,
`agent/dv-scan-hang-and-starvation`, `agent/policy-migration-audit` — all subsumed or superseded.
Close `fix/policy-tests-wall-clock`: both its author and the other session confirmed it is
redundant with `a88d541`.

**Merging `agent/dv-detector-consolidation` lands five branches at once** (verified with
`git merge-base --is-ancestor`, not assumed).

## 3. Two operational facts that are easy to get wrong

**The working tree is the deployment surface.** `ScanHound-DVScan` executes
`X:\Docker Apps\ScanHound\scripts\run-dv-scan.ps1` from the working tree, so **checking out a
branch deploys it**. `main` has no such file, so checking out `main` in the main working tree
breaks the scheduled task. **Use a worktree** for anything based on `main`.

**The container cannot read `dv_host.db` while the detector holds it open.** SQLite's WAL index
(`-shm`) needs mmap semantics the Windows bind mount cannot provide, so the read fails with
`disk I/O error` — and `import_dv_host_db()` catches that and returns
`{"imported": 0, "updated": 0}` **behind an HTTP 200**. Isolated with a controlled writer: writer
holding the connection → fails; writer exits → succeeds. This is why the container's `dv_scan` has
been frozen at 466 rows since 2026-07-26 while the host store kept growing.

**So never verify an import with an HTTP 200. Verify it with a known row.**

## 4. Outstanding, needing a person

1. **Round 4** on the DV consolidation — one substantive question: is close/reopen the right layer
   for the WAL problem, or should `dv_host.db` leave WAL entirely (~6 rows/hour workload), or
   should the detector POST its rows in the body so the container never reads the file?
2. **The canary** — existing roots only, no root widening in the same event, and do **not** restart
   the container first (the deliberate startup-baseline rule would adopt the new generation and
   sync nothing).
3. **Plex DV labels have been stale since 2026-07-26.** The fix exists; no canary has run.
4. **The Turnstile fold** — fully specified in
   `docs/reviews/peer-rounds/turnstile-fold-instructions.md`. One branch author offered to peer
   review the result rather than author it.

## 4a. A reviewed mass write is STAGED AND APPROVED, not executed

**Do not discover this by finding the artifacts.** 716 titles were proved FEL from a 1000-frame
bounded sample across the local 4K drives; 711 match a live Plex part, and 5 are individually
enumerated no-Plex-target rows. **Expected mutation: +711 `DV FEL`, 0 replacements, 0 removals.**

Four safety gates were signed off by peer review, and all 716 were re-verified through the
consolidation's **corrected** parser (716/716, 0 disagreements), so the staged set survives the
`_classify` tightening described in §1.

Artifacts: `docs/reviews/peer-rounds/dv-evidence-2026-08-10/` on
`agent/dv-gate-evidence-for-consolidation` — including **`label_snapshot.json`, the rollback
pre-image without which the write is not reversible**, and `staged_fel_apply.jsonl`, the rows
themselves. Procedure is §8 of `dv-scan-deploy-checklist.md`; §8c now requires confirming no scan
holds `dv_host.db` open and **verifying the import by a row count, never by the HTTP status**.

**Order matters: deploy → canary → write → widen roots, separately.**

## 4b. The 711-target FEL write is the local-drive WIDENING, not the canary

Verified on the committed evidence: all 711 staged rows have `source_path` on **local drives**
(`G:` `U:` `I:` `R:` `E:` `A:` `Q:` `J:`) — **none** are from the `Y:`/Magellan roots the
scheduled scan actually covers. So the write and the canary touch different libraries, and the
write is effectively the coverage expansion.

ChatGPT's canary instruction was to keep the existing roots and **not** combine detector
consolidation with the local-drive expansion. That separation is therefore not merely advisable —
these are two distinct operations on two distinct sets.

**The evidence is committed and the write is reversible**, which was a hard condition of the
sign-off. On `agent/dv-detector-consolidation` under
`docs/reviews/peer-rounds/dv-evidence-2026-08-10/`:

| file | what it is |
|---|---|
| `label_snapshot.json` | **Gate 4's rollback pre-image**, 711 entries |
| `staged_fel_apply.jsonl` | the 711 rows to write (a pretty-printed JSON **array**, not line-delimited) |
| `dv_host_rows_before.json` | rollback for the two rows already written live |
| `reverify_716.jsonl` | the 716/716 revalidation the sign-off rests on |

Independently checked here: **711 staged rows ↔ 711 snapshot keys, exact 1:1 with no extras**, and
**0 targets already carry a managed label** — so the write is purely additive and overwrites
nothing. These files existed only in a temp directory until they were committed; without
`label_snapshot.json` the write would not have been reversible.

## 4c. Do NOT merge the consolidation before round 4 answers

Round 4 is an **open question, not a verdict**. It asks whether close/reopen is the right layer
for the WAL problem, or whether `dv_host.db` should leave WAL entirely (a ~6 rows/hour workload),
or whether the detector should POST its rows in the request body so the container never reads the
file at all. **Merging now would commit `main` to close/reopen before the reviewer has answered**,
and two of those three answers change the shape.

Nothing is lost by waiting: everything is pushed, the evidence is committed inside the
consolidation, and `consolidation → main` shows **0 conflicts** — one merge lands everything when
the answer arrives.

## 4d. `.mp4` is declared supported but `dovi_tool` cannot read it — 2 files, non-urgent

**Earlier framing retracted.** `Twisters (2024).mp4` is **not** a third instance of the wedge. It
was probed directly: `dovi_tool extract-rpu -l 1000` returns in **0.2 s** with `rc=1`, no RPU, and
`Error: Invalid input file type`. It is a container-support gap, not a parser hang.

`_SUPPORTED_EXTS` declares `.mp4` supported, so such a file passes the extension gate, fails
extraction, and is filed as `unknown` — "detection could not run". Technically true, but the wrong
category: it is not transient, it is a container the tool structurally cannot demux. The row gets
a NULL signature and is therefore **retried on every run, forever, never converging**.

**Scope: exactly 2 files** across all four roots — `Twisters (2024).mp4` and
`Billie Eilish The World's a Little Blurry (2021).mp4` — against 647 `.mkv`. At 0.2 s per attempt
the cost is nil and retry backoff caps it, so this is **not a blocker and not urgent**.

**If it is fixed, two details matter** (verified against the consolidation, not assumed):

- Dropping `.mp4` from `_SUPPORTED_EXTS` works, but via `_iter_files`, which gates the walk on that
  same constant — the file is then **never yielded at all**, so there is no row rather than a
  better-labelled one.
- The `unsupported container` branch in `detect_layer` is therefore **not** what a scan would
  reach, and it returns `LAYER_UNKNOWN` anyway — so it would not have fixed the classification.
  It matters only for callers that do not pre-filter.
- The two existing NULL-signature rows would **remain** in `dv_host.db` unless separately settled.
  Harmless (an `unknown` row is non-authoritative and can never remove a label) but stale.

**Good news for the canary:** the walk finds 649 files across the four roots and `dv_host.db` has
**649 rows** — every file has a row, and the 236-file backlog is gone. So the canary runs against
**converged roots**, and any new NULL signature that appears is genuinely new rather than backlog
noise. The two real wedges (`Death Wish 3`, `Jurassic World Rebirth`) remain the only known
instances of the hang.

## 5. Detail lives here

- `docs/reviews/2026-08-10-scanhound-consolidation-map.md` — every current branch, attributed to
  its owning session, with verified ahead/behind and conflict counts
- `docs/reviews/peer-rounds/turnstile-fold-instructions.md` — the fold, port-by-port
- On `agent/dv-detector-consolidation`: `dv-detector-consolidation-round{,2,3,4}.md`,
  `2026-08-10-dv-round-summary.md`, the gate results, and the staged-set revalidation
- `X:\Docker Apps\SCANHOUND-CLOUD-SESSIONS-CATCHUP.md` — the running cross-session log (outside
  the repo)

## 6. The DV root cause, and the dead ends — do not re-derive these

The whole DV effort started from "extractions hit the 30-minute timeout, so scale the timeout by
file size." **That premise was wrong and the obvious fixes are all disproved.** Anyone meeting a
DV timeout will reach for one of these; each already cost hours.

**Throughput was never the problem.** Healthy files run **57–153 MB/s** end-to-end against a
storage path that streams **145–221 MB/s** — including 221 MB/s reading straight across the byte
offset where extraction freezes. At the slowest observed healthy rate the 30-minute cap covers
103 GB, more than the largest file in the library.

**The actual fault: two Profile 7 FEL titles wedge `dovi_tool`.** Sampled on the live process over
a 60-second window: **0 bytes read, 0 read operations, 95.7% of one core**, output file still 0
bytes, after reading 27.37 GB of 74.3 GB. A hang, not slow progress.

| do not try | why it fails |
|---|---|
| **Scale the timeout by file size** | The file is frozen, not slow. A bigger cap grants a wedged file *more* time — it would take the loss from ~1 h per run to ~3 h. |
| **Upgrade dovi_tool** | **2.3.3 does not fix it.** Tested because its changelog reads like the symptom ("extract-rpu now properly exit with errors for invalid inputs"). It hangs 2,465 bytes from where 2.3.2 does. |
| **Blame SMB / the network** | The whole file was copied to local NTFS. It stalls there too, *faster* (377 s vs 505 s) because the local read reaches the poisoned offset sooner. |
| **Test with a truncated copy** | `dovi_tool` rejects a truncated MKV in 0 s with `rc=1`, which reads as "completed without stalling" and produces the opposite conclusion. Use the complete file. |

Five stalls inside a **~68 KB window** across two versions × two storage paths. Frame-bracketed by
bisection: Jurassic World Rebirth completes at `-l 68018` and hangs at `-l 69577`; Death Wish 3 at
80,487 / 82,046. **Cause still unknown.** Report drafted at `docs/reviews/peer-rounds/dovi-tool-extract-rpu-hang-report.md`
**on `agent/dv-detector-consolidation`** (not on `main`), not filed.

**The fix that works** is a bounded read: `-l 1000` answers both wedged titles in 3–20 s and both
are FEL. Validated 22/22 against titles with known full-pass labels. Only FEL may short-circuit —
a bounded sample containing FEL *proves* FEL, while a sample showing only MEL proves nothing.

### Coverage, measured

Two closed questions, so they are not re-investigated:

- The 197 files in plain `4K` folders beside the scanned `4K DV` folders are **genuinely non-DV** —
  12-file random sample across all four drives, 0 with any RPU.
- **`4K HDR Colombo` is 11,547 TV files**, not movies. Correctly out of scope.

Still open: **2,827 unscanned 4K movies (147 TB)** on local volumes, only 87 ever scanned. Plex
addresses them by junction path (`C:\4K Drives\...`), which ScanHound does **not** resolve — a
config using drive letters would fail *silently*, the same shape as the 2026-07-11 incident that
lost all 371 `Y:`-drive files. Proposal in `dv-coverage-widening-proposal.md`.

### Known-unverified, carried forward

1. The mixed `(MEL, FEL)` case is **unvalidated by construction** — no such title appeared in the
   22 ground-truth samples. That is *why* only FEL short-circuits.
2. **~8 more wedged files are likely** among the 2,827 unscanned, extrapolating 2-in-730. The
   bounded probe would not have caught them; it only reads the first 1,000 frames.
3. The `Profiles:` plural spelling is **latent, not confirmed live**. A binary string search returns
   zero — but also returns zero for `Profile: `, which the binary demonstrably prints, so that zero
   proves nothing.

## 7. The methodological note

Three times during this consolidation a **green test suite was actively concealing a real
defect**: a test that asserted the unsafe `none`; a suite that passed only because of pytest's
collection order; and an HTTP 200 that meant zero rows imported. Each was found by looking at live
output rather than by reasoning about the code.

The related habit, from the other session, while re-verifying a result it had already correctly
predicted: **"predicted by the evidence" and "measured" are different claims.**

---
