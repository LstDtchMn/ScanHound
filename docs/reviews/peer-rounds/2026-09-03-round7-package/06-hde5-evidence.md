# HDE-5 evidence — 2026-09-05

Worktree `C:\Users\NLSur\AppData\Local\Temp\hde5`, branch `docs/hde5-hdencode-docstrings`, stacked on #113 @ `436819d` (→ #109 → main). Head: 6aa9774. Docs only.

## 1. Scope and rules

The peer reviewer's order (download_service → source_health → coordinator and hold → database → action service and queue → glue) and vocabulary (reveal · transport attempt · observation · diagnostic · health outcome). Round-7 rules: current behaviour stated mechanically; historical marked historical; the owner of each decision named; no "only / same / never / always" unless the code enforces the predicate; no ticket ids in production docstrings. `configure()` and the recovery predicate in the coordinator were left untouched because #112 rewrote them on another branch.

## 2. Docs-only, proven

For every changed file, docstrings were stripped from the committed version and the working copy and the syntax trees compared: identical for all ten files. (The first run of the checker reported five spurious differences: it decoded `git show` output in the locale encoding, cp1252, which turned pre-existing non-ASCII characters in string literals into mojibake on the "before" side only. Fixed to decode UTF-8 explicitly; rerun clean.)

## 3. Stale claims replaced (with the code line that contradicted each)

| where | the stale claim | what the code says |
|---|---|---|
| `download_service.py` `scrape_links` | "service_type: 'Rapidgator' or 'Nitroflare'"; "Returns: List of download link URLs" | `_host_keywords` (~3153) supports rapidgator, nitroflare, 1fichier, ddownload; an unknown value logs a warning and defaults to rapidgator (~3156-3158); the return is a `ScrapedLinks` (list subclass) whose `.diagnostic` drives accounting and health |
| `download_service.py` `_log_page_diagnostics` | one line, naming none of the three call-site stages nor who owns each diagnostic | three stages (`access_control`, `requested_host`, default page); `health_owner="coordinator"` only for `INTERACTIVE_CHALLENGE` (2572) and `REVEAL_VERIFICATION_STALLED` (2618) when `source_kind == "hdencode"`; every other return in this function keeps the dataclass default `outcome_recorder` (`scrape_outcome.py:206`). (Elsewhere in the module `scrape_links` sets `coordinator` for `SOURCE_DISABLED` at 3023 and `none` for direct-link/unsupported at 3091/3104; `download_outcome.py:449,470` sets `coordinator` for the traffic-denial diagnostics; the docstring is scoped to this function) |
| `download_service.py` `download_item` | nothing distinguished `source_reveal_succeeded` from `source_progress` | `source_reveal_succeeded` is `bool(links)` captured right after the boundary returns and before the direct-link fallback (~4122, 4157), the flag the queue's hold release keys on (`download_queue.py:1219`); `source_progress` is set only at JDownloader/clipboard/browser delivery (~4206, 4244, 4261) and feeds the queue's retry budget |
| `download_service.py` `scrape_links_recorded`; `hdencode_action_service.py` `run_action` (no docstring before); `download_queue.py` five methods (no docstrings before) | ticket labels (HDE-3, HDE-4) in place of concepts; the unit of accounting unstated | the reveal-boundary invocation is the unit; an observation does not imply a transport attempt; `refused` covers invocations stopped before transport; the accounting call is guarded so its failure cannot change health, hold release or the return value |

## 4. Truth-check (Sonnet, read-only; the Opus run was cut off by the session limit), every added sentence against the code

Twenty-six claims across the ten files checked with file:line citations; all TRUE of the code, including the ones the supervisor had flagged as highest risk: the `health_owner` split in `_log_page_diagnostics` (scoped to that function; the module's other assignments listed above); every coordinator `observe_*` call in `scrape_links` and its helpers gated on `source_kind == "hdencode"` (seven call sites; `_navigate_with_diagnostic` gates on a local computed the same way); `download_item`'s two flags; the coordinator paragraph's "never reads accounting" (grep: the table and read API are referenced only by `database.py`, the boundary write, the sources route and the tests); `_query_dicts_strict` never returning `[]` on failure; no retention code (no `DELETE FROM hdencode_reveal_observations` anywhere); `record_scrape_outcome`'s code-match branch has no source gate (true "for any source"; today's only caller passes `hdencode`).

Findings, all actioned before commit: (1) the supervisor's relocation of the coordinator paragraph from the class docstring to the module docstring (to avoid an adjacent-hunk conflict with #112) left four cross-references pointing at the class docstring; (2) "N attempts produce N observation rows" overstated: `download_item`'s two dedup short-circuits return before the boundary; (3) the hold check's "cooldown, challenge or rate limit" omitted "disabled" and a persisted health block; (4) "for any source" given the today-only-hdencode caveat; (5) three surviving ticket ids ("HDE-3, round 7b" in `download_item`; "(HDE-4)" on the table; two older review-round labels in the hold-release intros) replaced with concepts or "historically". Vocabulary audit: no violations after the relocation (one transient "request" vs "transport attempt" inconsistency was already reworded). No aspirational statements.

## 5. Suites

Twelve focused files in CI's collection order (alphabetical): 753 passed. Real root absent. Note on the lineage: this branch does not carry #111, so if `test_scrape_outcomes.py` runs before `test_queue_review_followups.py` in one process the TST-2 leak reproduces here (2 failed, 390 passed in the lane's non-alphabetical run); in CI's order it does not. That is the known TST-2 defect, fixed on #111, not an effect of this change; docstrings are inert at runtime.

CI on `194ba8c` (ubuntu-latest): Tests workflow green on Python 3.11 and 3.12, frontend green. CI VERIFIED.

## 6. Merge hazard with #112, rehearsed

The docstring lane first appended the coordinator paragraph to the class docstring of `HDEncodeTrafficCoordinator`; #112 inserts a comment block immediately after that docstring (before `_BLOCK_STATUSES`), so a three-way merge would have touched adjacent lines. The supervisor moved the paragraph to the module docstring at the top of the file, which #112 does not touch, and the truth-check confirmed the class docstring is back to its one-liner.

Seven-branch rehearsal on a scratch worktree of `main` @ `0a2751d`, `--no-ff`, in merge order with #112 merged before HDE-5:

```
CLEAN  #108 2e91de0 → #109 3abb575 → #110 6ae62dc → #111 1db4ac4 → #112 061a6a0 → #113 436819d → HDE-5 194ba8c
rehearsal head 4517a88 (discarded); nothing pushed
```

No conflicts. (The integrated six-branch stack's full suite, 5543 passed with the real root absent before and after, was run earlier today; HDE-5 is docs-only on top of it and adds no tests.)

## 7. Peer review of 194ba8c: conditional pass, five sentence-level corrections (HDE5-R1 to R5)

The reviewer accepted the architecture, found no behaviour change, and asked for five truths, each applied with the reviewer's wording in the second commit 6aa9774:

1. `source_progress` is downstream delivery progress (set only after JDownloader, clipboard or browser delivery succeeds), not a crossing of the source boundary; the source reveal is captured earlier as `source_reveal_succeeded`, before the direct-link fallback, and that is what hold release reads.
2. Accounting is fail-soft, so "exactly one row per invocation" was false: one write attempt per qualifying invocation; one row appended when the write succeeds; a failed write leaves no row, logged and swallowed. A grep of the ten files for "one row / exactly one / one observation row" found eight persistence-as-certain statements across `database.py` (the table comment, the recorder docstring, the CREATE TABLE comment), `download_service.py` (the boundary), `download_queue.py` (`_execute`) and `hdencode_action_service.py` (`run_action`); all now conditional on the write succeeding. Remaining hits are unrelated tables or the coordinator's `request()` ("authorize exactly one transport operation"), untouched.
3. Verification holds are armed and released by the queue/database hold path from scrape outcomes and the affirmative `source_reveal_succeeded` fact, not by the coordinator's live state; neither path reads accounting.
4. An RSS retry reuses its action id; only a retry that reaches the boundary may append an observation. Confirmed in `run_action`: `claim_hdencode_action` at line 188 succeeds, the `if cancelled():` check at line 208 runs, and `scrape_links_recorded()` is called at line 222, so a cancelled retry never reaches the boundary.
5. The daily volume figure is an estimate: refused observations are recorded with no transport attempt and the table has no daily limit or retention policy.

Rerun: docs-only proof identical across the five touched files; twelve focused files in CI's order 753 passed (twice: the lane's run and the supervisor's). CI on `6aa9774` (ubuntu-latest): Tests workflow green on Python 3.11 and 3.12, frontend green. CI VERIFIED.
