# Claude — Master Plan Revision 2 acceptance confirmation (2026-07-19)

Follow-up to `CLAUDE_MASTER_PLAN_REVIEW.md` (verdict: ACCEPTED WITH REQUIRED
CHANGES). ChatGPT returned Revision 2. This note records that the four required
changes were **verified in the revised documents themselves**, not accepted from
the disposition table.

Rev2 ZIP SHA-256 `5a5e761…c8c26f2ab` — verified. All 9 files verified against
`SHA256SUMS` — OK.

## Required changes — verified landed

| ID | Requirement | Verified in Rev2 |
|---|---|---|
| B1 | Delete the dormant obscuring options; do not relocate into the coordinator | MP-15: "Delete the three options outright; do not route or reproduce them in the coordinator or live path." Network Invariant 6: "deleted, not relocated." PR C acceptance: "deleted and absent from all coordinator/live paths." The Rev1 "route through DownloadService/coordinator" disjunction is removed. |
| B2 | Coordinator enforcement as a tested gate | MP-04 adds "a constructor-authorization enforcement test." Coordinator contract: "monkeypatch every direct `webdriver.Chrome`, `_ensure_selenium`, and relevant HTTP-client constructor to reject construction outside the coordinator authorization context." Present in PR C acceptance, Phase 4, DoD, and TEST_MATRIX with "assert zero unauthorized construction." |
| B3 | SH-R04 = bookkeeping/false-success, not media loss; does not block #15/#16 | MP-07 reclassified: "not the SH-R02/R03 media-loss class because the file is already restored or intentionally deleted … does not block #15/#16 deployment." RISK R-07/R-08 carry the same distinction. |
| B4 | Separate no-replace from crash durability | MP-09: "No-replace may be supported, but manifest crash durability cannot be promised on CIFS and is degraded on Windows … publish separate no-replace and durability guarantees." RISK R-10 (CIFS) and R-11 (Windows) split into two rows. |

## Evidence gates — correctly preserved

- **MP-09 (CIFS durability):** remains an explicit Auto-rename hold gate; no
  durable-persistence claim before the sentinel runs. Correct.
- **MP-10 (PR #17 late worker):** "a generation token is prohibited as
  speculative scope unless the defect reproduces." Stronger than requested and
  exactly right — reproduce first, add machinery only if warranted.

## Simplifications and no-change items

All adopted as reviewed: lifetime single-writer lock file under the file-safety
track; reuse of existing size/resolution rules behind a claim-aware layer;
`asserted|negated|unknown` + `hdr_formats[]`; rebuild #8 as PR D; keep #14/#17
independent; #15→#16 stacked; RSS sequence E→F→G→H.

## Nothing weakened

Independent check confirms no item I flagged was silently dropped or softened,
and the revision introduced no scope expansion beyond the four changes and the
graph edit. The changes are surgical and match the review verbatim.

## Disposition

Revision 2 fully incorporates the required changes. No further plan-level changes
are requested from me. The plan is clear to proceed to phased implementation,
subject only to the two open evidence gates above, which are Jesse-authorized /
reproduction-first respectively — not plan defects.

State unchanged: PR #15 `70dca70`, #16 `44ea7ba`, #17 `bf07697` draft; RSS
foundation branch empty at `555e26b`; `main` `555e26b`. Nothing merged, deployed,
force-pushed, or marked ready. Auto-rename still enabled in production per live
verification; not changed by this review.
