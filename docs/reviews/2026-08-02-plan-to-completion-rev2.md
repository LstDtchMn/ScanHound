# ScanHound — plan to completion, revision 2

**Date:** 2026-08-02 · **Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
**Supersedes:** `2026-08-01-plan-to-completion.md`

Rev 1 was reviewed twice. The first pass was written without reading the
document and is void as evidence. The second pass read the package and the
repository, confirmed most conclusions, revised three, and found four internal
contradictions in rev 1 that were mine. This revision applies all of it.

**Jesse's decisions, already taken:** split qualification into Phase A and
Phase B; fix the parser divergences, the harness gaps and this plan before
anything else.

---

## 1. What changed from rev 1

### Accepted without change (R1, R2, R3 core, R5 direction)

* **R1** — retire the atomic snapshot of void evidence; keep an ordinary
  rollback copy labelled *NOT ADMISSIBLE QUALIFICATION EVIDENCE*.
* **R2** — B1 drops to a short historical-limitations note and no longer gates
  B7. Do not infer any success or failure rate from pre-ledger rows.
* **R3** — B2 is **closed**; B3, B5 and B6 remain, bounded. The freeze is now
  described as *"no known live data-loss defect; rename stays disabled until the
  fixes are demonstrated under the real storage topology and interruption."*
* **R4** — the calendar-vs-consequence distinction holds. It fails only if
  "start the clock" is used to justify freezing in known defects, which is
  exactly why the parser fixes come first.

### Revised

**R5 — the freeze boundary is an artifact identity, not a file list.**
My named-file list was insufficient. A new image can alter imports,
initialisation, dependency versions, startup order, shared models or database
setup regardless of which feature it was built for, so *"the rename code is
switched off"* does not make deploying it harmless. The boundary is:

> **exact image digest + exact runtime/container configuration + locked
> thresholds and external inputs.**

*Allowed during a window:* restarting the **same digest** with unchanged
configuration, recording the interruption, and demonstrating missed-poll and
catch-up recovery.
*Window-invalidating by default:* any new image; any Compose, environment,
mount, dependency, base-image or entrypoint change; any migration or startup
change; any parser, canonicaliser, policy, persistence, readiness or collector
change.

Practical consequence: **do not overwrite the qualification image's `latest`
tag while building Track B/C/D.** Use separate immutable tags and restart the
qualification container without rebuilding.

**A4 / R6 — the property is semantic equivalence, not identical output.**
The two paths may legitimately differ in raw metadata, provenance, timestamps,
feed categories and HTML-derived detail. For the same release, policy and
library state they must agree on: normalised identity, eligibility, media type,
year/season interpretation, quality class, final action class, action
eligibility, and any material rejection or ambiguity reason. That is an
equivalence contract, not byte equality.

**(d) `S104` does not have to mean "support season 104."** Both paths rejecting
it as ambiguous under one shared grammar is an equally valid resolution — and is
what was implemented, because silent truncation to 10 is a confident wrong
answer.

### Corrections to rev 1 that were mine

| Rev 1 said | Reality |
|---|---|
| Scorecard cited `aefa841` / `5bf51fb` | Those were already superseded when I wrote it |
| Phase 0 listed "build the A4 harness" and "re-run B2" | Both had already been done and were recorded in the same document's addenda |
| `#184` appeared in the Track D scorecard | …but nowhere in the phased route, so it had no path to closure |
| Phase 2.10 deployed Track D during the window | Directly contradicts the exact-digest rule the same document accepted |
| B5 and B6 shown as Claude-owned and ungated | They involve real storage and real container interruption; both need Jesse's authorisation |
| B2 doc: `_move_no_replace` raises `UnsupportedFilesystemSafetyError` | It raises a plain `OSError`; the custom class belongs to the fsync preflight. B5 would have caught nothing and reported success |
| A4 doc: "nothing detects the next instance" | `tests/test_rss_full_disc_symmetry.py` already covers full-disc symmetry |

---

## 2. The qualification split

Rev 1 treated qualification as one seven-day window. It cannot be, because the
listing path never enters the candidate decision pipeline, the shadow
comparison stops at URLs, and there is no shared decision object to compare.

**Phase A — acquisition qualification.** Seven days, starts once the candidate
is pinned. Establishes: URL acquisition coverage, latency, feed health, request
reduction, population symmetry, restart and catch-up recovery, RSS processing
completeness, and corrected field-level parity.

**Phase B — decision suitability.** Its own predeclared evidence plan against
its own pinned artifact. Establishes semantic decision equivalence over the
Phase A corpus and suitable live or replay evidence.

**T2 splits:**

* **T2a — processing completeness.** Zero actionable RSS candidates left
  unresolved at Phase A closure.
* **T2b — semantic decision parity.** No material decision mismatches under the
  predeclared equivalence contract.

**Three things that would invalidate the split:** calling Phase A alone "RSS
qualification"; scoring T2a as if it were A4; or promoting after Phase A.
Promotion requires both phases.

Phase A evidence carries into the promotion decision **only if** the Phase B
artifact does not alter acquisition semantics. Any change to RSS discovery,
listing discovery, policy, canonical URL identity, cadence or population
construction invalidates Phase A and requires a fresh window.

---

## 3. Route to completion

### Phase 0 — finish and pin the candidate  *(current phase)*

| | Item | Owner | Status |
|---|---|---|---|
| 0.1 | Fix divergences (a)(b)(c)(d)(e) in one shared grammar | Claude | **done** — `backend/release_grammar.py` |
| 0.2 | Route both production readers through it; delete the duplicate patterns | Claude | **done** |
| 0.3 | Replace the harness's test-only transcription with production calls | Claude | **done** |
| 0.4 | Add media-type parity using listing `mode` + RSS categories | Claude | **done — and it found a sixth divergence** |
| 0.5 | Re-run B2 reproductions | Claude | **done** — pre-fix loses data, current does not |
| 0.6 | Correct this plan's contradictions | Claude | **done** — this document |
| 0.7 | Approve T1, T2a, T2b and the Phase A/B contract | 🔒 Jesse | open |
| 0.8 | Merge the sweep candidate to `main` | 🔒 Jesse | open |
| 0.9 | Build **once** under an immutable tag; record digest + config fingerprint | 🔒 Jesse | open |
| 0.10 | Diagnostic-only rollback DB copy | Claude | open |
| 0.11 | Deploy exactly that artifact | 🔒 Jesse | open |

**0.4 found a sixth divergence, and it was the most consequential of the six.**
Listing media type is `mode == 'tv' or is_tv_release(title)`; RSS media type
keyed purely off a parsed season. So four ordinary TV title forms —
`Complete Series`, `Mini Series`, `TV Series`, `Season 4` — were classified as
**movies** on the RSS path and TV on the listing path. `media_type` selects the
Plex library in `get_hdencode_candidate_context()`, so the same release reached
different actionable decisions depending on how it was found. That is the A4
failure itself, live, on the path being promoted.

It stayed invisible because the earlier harness compared four fields derived
from a title, and media type is not derived from a title alone — `mode` is
which category URL was crawled. A title-only fixture cannot express that, so the
comparison was never made. Feed categories usually masked it, since feeds are
per-category; "usually" was doing the work.

Fixed by `title_indicates_tv()` in the shared grammar, deliberately title-only,
with each path's out-of-band signal additive on top.

**Phase 0 now has no open engineering work.** What remains is Jesse's: 0.7
through 0.11.

### Phase 1 — bootstrap (~30 h, unattended)

Verify all three auto-flags still `false`; verify the readiness cross-check
succeeds in production (it has never once succeeded); run the per-source
bootstrap.

### Phase 2A — acquisition window (7 calendar days)

Start and lock the window (🔒 Jesse). During it:

* same-digest restarts allowed and logged; **no new production image**;
* Track B/C/D developed and tested but **not deployed**;
* **B3 runs in isolated CI.**
* **B5 and B6 run only in a separate pinned rename-test container** — separate
  test database, scratch paths, no production service control, no shared mutable
  application state.

| | Concurrent work | Owner |
|---|---|---|
| 2.1 | B1 bounded historical note | Claude |
| 2.2 | B3 fault injection, in CI | Claude |
| 2.3 | B5 capability probe + copy-only rehearsal | 🔒 Jesse authorises, Claude executes |
| 2.4 | B6 restart/reconciliation invariants | 🔒 Jesse authorises, Claude executes |
| 2.5 | Build the decision bridge (not deployed) | Claude |
| 2.6 | Track C: CI secret scanning, allowlist, response procedure | Claude |
| 2.7 | Track D: TV filter + 720p chip, full-disc setting in UI, HDR10+ labels (`#185`), documentary pass, `#192`, `#184` counters | Claude |

**B5's first stop condition is capability discovery, before the rehearsal counts
as started.** Per destination volume, record: mount identity, filesystem type,
mount options, `st_dev`; kernel, libc, exact test-image digest; whether
`renameat2(RENAME_NOREPLACE)` succeeds; if not, whether same-volume
`os.link(temp, dst)` succeeds; existing-destination collision behaviour;
directory-fsync behaviour; and the exception plus observed disk state when a
capability is missing. Scratch files only — no source-consuming operation is
needed to learn a capability. **Detect unsupported publication by errno
(`ENOTSUP`, `EOPNOTSUPP`, `ENOSYS`), never by catching
`UnsupportedFilesystemSafetyError`**, which is never raised on that path.
`filesystem_safety_status()` reports the literal `"renameat2_or_hardlink"` — it
says one of the two is expected, not which one works, so it is not evidence.

### Phase 2B — decision suitability

Grade and preserve the Phase A verdict first. Then build a new bridge-specific
image digest, run the predeclared Phase B plan, compare semantic decisions over
the captured Phase A corpus, and block promotion until T2b passes.

### Phase 3 — promotion verdict

Three steps, not one: acquisition verdict → decision-suitability verdict →
combined promotion decision (🔒 Jesse).

### Phase 4 — rename rollout

Own pinned image digest. B1–B6 documented pass → B7 authorised (🔒 Jesse) → one
backed-up sacrificial file → verify source, destination, hash, ledger, DB, Plex
visibility and restart recovery → gradual expansion under explicit stop
conditions (🔒 Jesse).

---

## 4. Jesse's gates

Beyond merge and deploy, which were always his:

1. Approve T1, T2a, T2b and the Phase A/B contract — **before** the window opens.
2. Approve the exact sweep image digest.
3. Approve a separate exact rename-test image digest.
4. **Authorise B5** before any scratch write on real storage.
5. **Authorise B6** before any real container interruption.
6. Start and lock the window.
7. Authorise B7 and any expansion.
8. The combined promotion decision.

Execution stays Claude-owned after each authorisation.

---

## 5. Track D's exit

A **batch** exit, not "product complete". Every listed item ends as one of:
implemented, tested, deployed and accepted; explicitly deferred to a numbered
issue with reason and priority; or rejected as no longer wanted. Once all have a
disposition, later product work is ordinary backlog and outside this plan.

Acceptance conditions: the TV resolution filter distinguishes 720p, 1080p and 4K
correctly; the full-disc setting is visible and reflects the same shared rule
both discovery paths use; HDR10+ metadata reaches labels and Kometa output; the
documentary work produces a recorded design decision; `#192` is corrected and
verified; `#184` counters correspond to actual lifecycle stages including
failure and cancellation.

**Programme completion** is separate: RSS-primary has a documented promotion or
non-promotion decision; rename B1–B7 has a documented rollout or
continued-freeze decision; security controls are completed or explicitly
accepted as residual risk; all destructive automation remains disabled unless
its gate has passed; and rollback and operating instructions match the deployed
system.

---

## 6. Accepted risks

* **T7 (coverage margin) is still the threshold most likely to fail on merit** —
  `tv_all` showed −0.12 h in the void window. It should fail rather than be
  waived.
* **Volume-anomaly detection does not operate.** `expected_typical` has no
  producer, the check is disabled, and no threshold depends on it. The
  qualification report must not imply the protection exists.
* **One deferred credential item remains unrotated**, by Jesse's decision. Not
  in the repository.
* **Public topology retained**, unchanged from rev 2.1 §4.
* **Phase A can pass while A4 is unproven.** That is the price of the split, and
  it is only safe because promotion is explicitly blocked until Phase B passes.
  The discipline has to survive a passing Phase A result.
