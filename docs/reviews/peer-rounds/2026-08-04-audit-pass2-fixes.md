# Peer review request — audit pass 2, 25 of 32 findings fixed

**Repository:** LstDtchMn/ScanHound
**Branch:** `agent/audit-fixes-pass2`
**Base:** `af9c299` (main)

Read the branch through the GitHub connector. **Review the code and the tests,
not this summary.** If you find yourself assessing my description rather than
the diff, stop and say so.

The full finding list, with each verifier's reasoning and the evidence that
survived an adversarial refutation pass, is in
`docs/reviews/2026-08-04-audit-pass2-findings.md` on this branch. Every heading
is marked ✅ FIXED / ⚠️ PARTIAL / ❌ NOT FIXED, or left unmarked when still open.

---

## Where the findings came from

51 agents across six subsystems, then an adversarial verifier per candidate
told to refute it. **32 confirmed, 13 refuted, 3 unverified.** The 13
refutations are recorded in the same document so a later pass does not spend
budget rediscovering them — and they are the reason to give the other 32 any
weight, because the verify stage demonstrably could say no.

Almost every finding has the same shape: **the operation reports success, the
log stays clean, and the damage only shows up later as missing data.** That is
why a codebase with ~4,300 passing tests still had 32 of them. The tests assert
what the code returns, and all of these return the right thing while doing the
wrong thing.

## The three I fixed and reproduced personally

- `38768fe` **CRITICAL** — `rematch_cache()` blanks every cached row's Plex
  state before matching, expecting the match to restore it. `stop_scan_flag` is
  set INTERNALLY on an ordinary Cloudflare block and cleared ONLY at the top of
  the next `run_scan`, so any re-match in between matched zero items and
  persisted the blanked rows: **the whole library cache rewritten as "missing",
  logged as a successful re-match.**
- `d749bb2` **HIGH** — three upserts ended `return self._mutate(...) is not None`.
  `_mutate` returns True/False, never None, so every failed write reported
  success and four live `if not ...` guards were dead code.
- `ed1e1ec` **HIGH** — a failed Regrab flipped a delivered download's row to
  `status='failed'`, so it stopped counting as downloaded and lost duplicate
  protection.

## Where I would attack this if I were you

1. **The `credential_state` rewrite (`backend/api/dependencies.py`,
   `backend/database.py`).** This is the one I changed after an agent wrote it.
   Their version bypassed `has_password()`, and because a `MagicMock` answers
   every attribute it resolved to "unknown" → fail closed → **401 across a large
   part of the suite**. I moved the three-state detection into
   `DatabaseManager.credential_state()`, where a sentinel can actually be passed
   into `_query`, and made the API layer fall back to `has_password()` for
   anything that is not one of the three known strings.
   **Is that fallback a hole?** My argument: a real `DatabaseManager` always
   returns a valid string, so the fallback is only reached by a stand-in that
   cannot report a read failure anyway. **Check whether a real deployment can
   reach it** — if it can, the fail-closed property is gone.

2. **`_listing_arm_incomplete` (`backend/background_scanner.py`).** This decides
   what counts as RSS promotion evidence, so getting it wrong corrupts a
   decision record. It uses the crawl's seen-set rather than `err`, because
   `run_scan` swallows every exception and a blocked cycle arrives with
   `err=None`. It deliberately does NOT treat an early-stop-at-cached-content as
   incomplete. **Is that the right line?** Too strict and the shadow evidence
   stalls; too loose and blocked cycles keep counting.

3. **The WAL work (`backend/config.py`, `backend/database.py`).** Two claims to
   check: that `checkpointed == log` (not `busy`) is the right completeness
   test, and that moving `-wal`/`-shm` with the quarantine backup is safe rather
   than merely tidier. A reader holding a CURRENT snapshot gives `(1, 53, 53)` —
   busy, but complete.

4. **`_scan_was_cancelled` (`backend/api/routes/scanner.py`).** It ignores a
   non-bool `stop_scan_flag` specifically so a Mock's truthy attribute is not
   read as the operator pressing Stop. **That is a test-shaped concern leaking
   into production code** — is it justified, or papering over a fixture problem?

## Attestation, and its limits

- Full suite: see the run recorded in the branch's CI. Locally, in a throwaway
  container with the code copied in (not the 9p bind mount) and byte-verified
  by raw sha256 before each run.
- **14 failures are PRE-EXISTING.** Not asserted — measured, by running the same
  suites against `git archive HEAD` (committed tree, zero agent work) in a
  separate container and getting the identical list: missing host-detector
  script, no selenium, headless desktop notifications, pre-existing doc drift.
- CI now runs on this branch at all, which it did not before: `main`'s workflow
  triggers only on `[main, master, develop]`.

**What I am least sure of.** Four of the five agents that produced this work
were killed by a session limit during their verification phase. I reviewed all
11 modified files line by line and ran the mutation checks one of them never
got to (each auth fix reverted individually; every mutation is caught). But
**the notification, Plex and DB-integrity tests have not been mutation-checked**
— they pass, and I have read them, and I have not proven they discriminate.
Treat those three files' green as weaker evidence than the rest.

## Not fixed, deliberately

Six findings remain open and are listed with their costs in the findings doc.
One (#19) is marked NOT FIXED rather than quietly closed: the corruption flag is
still consumed whether or not the alert was delivered, and the docstring now
describes that as intentional. The finding disagrees. I left the disagreement
visible instead of resolving it by fiat — **your call is welcome.**
