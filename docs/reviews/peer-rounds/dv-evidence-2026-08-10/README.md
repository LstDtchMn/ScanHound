# DV round evidence — 2026-08-10

Every document in `docs/reviews/peer-rounds/` that says `scratchpad/<file>`
means a file in **this directory**. They were produced in a session-scoped
temporary directory and committed here before that session was archived,
because two of them are not reproducible without re-running hours of scanning
and one of them is the only thing that makes a live write reversible.

## The two that must not be lost

| file | why |
|---|---|
| `label_snapshot.json` | **Gate 4's rollback pre-image.** The managed DV label state of all 711 target titles *before* the write. Without it the mass write is not reversible, which was a hard condition of the sign-off. |
| `dv_host_rows_before.json` | Rollback for the **two rows already written live** (Death Wish 3, Jurassic World Rebirth). Restore by setting `dv_layer='unknown'` and both signature columns to NULL. |

## The staged operation

| file | contents |
|---|---|
| `staged_fel_apply.jsonl` | **The 711 rows to write.** Each carries `source_path`, the rewritten Plex-form `path`, `dv_layer: fel`, `evidence: bounded`, and the matched `plex_id`. |
| `staged_fel_no_plex_target.jsonl` | The 5 enumerated no-Plex-target rows, deliberately excluded so that *rows intended for Plex effect == rollback snapshot population*. |
| `reverify_716.jsonl` | Re-verification of every positive through the **consolidated** parser: 716/716 still FEL, 0 disagreements. The sign-off rests on this. |

## Raw measurements — expensive to reproduce

| file | cost to regenerate |
|---|---|
| `local_quickcheck.jsonl` + `local_quickcheck_part1.jsonl` | ~110 min of scanning. Bounded probe over 2,738 local 4K movies; 716 FEL (26%), 0 errors. |
| `sweep_results.jsonl` | ~13 min. The 149-file sweep of the configured roots that wrote 67 proven-FEL rows. |
| `bounded_results.jsonl` | The 22-title ground-truth validation (8 FEL, 8 MEL, 3 P8, 2 P5, 1 none) with full `dovi_tool` summaries — the evidence that bounded FEL is safe, and the reason `Profile: 8 (FEL)`-style inputs were known not to occur. |
| `local_4k_inventory.json`, `unscanned_movies.json` | ~20 min of walking. The coverage measurement: 5,292 files, 4,996 distinct titles, 2,827 movies ≥15 GB, only 87 ever scanned. |

## What is NOT here

The 74 GB local copy of `Jurassic World Rebirth` used to eliminate SMB from the
dovi_tool hang lives at `F:\_dvbugtest\jw_full.mkv`. It is **not** in the repo
for obvious reasons, and it is safe to delete — the result it produced is
recorded in `dovi-tool-extract-rpu-hang-report.md` (stalls at byte
27,367,130,713 on local NTFS, versus 27,367,062,473 over SMB).
