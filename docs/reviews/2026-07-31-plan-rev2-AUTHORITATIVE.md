# ScanHound plan — revision 2.1, AUTHORITATIVE

**Date:** 2026-07-31 · **Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
**Revision:** 2.1 — incorporates ChatGPT round-2 review of blob `7e42598`
(verdict: *substantially correct, revision 2.1 required*). All ten required
edits applied.

**Supersedes:** `2026-07-31-decisions-and-plan.md` and `2026-07-31-decision-record.md`
(both at `ade5348`). Written in response to ChatGPT's review of that commit,
verdict *PLAN REQUIRES REVISION BEFORE EXECUTION*.

## Precedence

**This document controls.** Where it conflicts with the two superseded files or
with `C:\DockerData\infra-ops\DECISIONS-2026-07-31.md` (Jesse's private copy),
this document wins. The superseded files are retained only as the record of what
was believed before review; the private copy is being corrected to point here.

**Jesse's 22 decisions are unchanged.** The review corrected *reasoning,
wording and ordering*, not intent. Nothing below reopens a decision Jesse made.

**Guardrails unchanged:** merge, deploy, force-push, production settings and
feature enablement are Jesse-only. Auto-rename and auto-grab stay off.

---

## 1. Corrections accepted

Every blocking item from the review is accepted. Six changed my conclusions,
not just my phrasing.

### 1.1 Full-disc: direct denial withdrawn, indirect path accepted

**Corrected again in rev 2.1.** Rev 2 said full-disc "cannot produce
`listing_only` misses" — too absolute. The controlling statement is now:

> **Full-disc releases cannot _directly_ appear as `listing_only` misses,
> because they are absent from the post-policy listing comparison set. They may
> still _indirectly_ worsen RSS coverage if they consume positions in a finite
> upstream feed window and displace relevant non-full-disc releases.**

The direct case is settled: `compare_shadow()` computes
`listing_only = listing_urls − rss` on the listing result **after** policy
filtering, so a full-disc URL can only ever land in `feed_only`.

The indirect case is real and neither ChatGPT nor I had it before round 2. If
each normal feed returns at most ~50 entries, full-disc entries occupy slots.
A wanted release pushed past the window boundary is present in the listing,
absent from the finite RSS response, and lands in `listing_only`. The full-disc
URL never appears there — but it helped put something else there.

**Two separate concerns, never to be conflated again:**

| | Concern | Fixed by |
|---|---|---|
| **Population parity** | Listing and RSS apply the same policy | `#191` |
| **Window sufficiency** | Feeds deep enough, or polling frequent enough, that wanted releases do not age out | **Not `#191`** |

**`#191` cannot repair finite-window displacement.** Filtering full-disc out
*after* the feed is fetched does not make HDEncode return the 51st entry. Any
claim that it does is wrong, and this document does not make it.

### 1.2 "97 titles" was never supported by the measurement

The tooling sums `relevant_miss_count` per cycle. The correct wording is
**97 cumulative relevant-miss observations**. That is not 97 titles, 97 URLs, or
97 unresolved releases — one persistent URL missed across many cycles produces
many observations. All prior wording of mine is withdrawn.

### 1.3 Collector networking — I was wrong, verified

I told Jesse the collector runs on the Windows host and fails because no host
port is published. **Verified false.** `collect_shadow_evidence.py` runs the
evidence step as `docker run --rm ... --entrypoint python IMAGE
/scripts/05_shadow_evidence.py --base-url http://127.0.0.1:9721`.

`127.0.0.1` there is that ephemeral container's own loopback — not the host, not
ScanHound. Right symptom, wrong mechanism, and the mechanism mattered:
**publishing a host port would not have fixed it.** The container is also given
no `--network proxy`, so it cannot reach `scanhound` by name either.

Fix: attach the collector to the network ScanHound is on and address it by
service name and internal port.

### 1.4 A better hypothesis for the constant 100

ChatGPT's reading — **two feeds each returning a 50-entry window** — is more
specific than my "fixed-length feed" and is directly testable. The parser
permits more than 50 per feed, so 100 is not a parser ceiling. A shallow sliding
window that ages out releases during busy periods is now the leading candidate
and must be measured, not assumed.

### 1.5 The rename defect claim was too broad

"Reproduced TOCTOU data-loss defect in file placement" was inherited from an
earlier session and never re-verified against the current commit, which performs
a destination precheck, atomic `os.link` for hardlink publication, verified
temporary copy plus no-replace publication for copies, and guarded destination
replacement. **The claim is withdrawn** pending an exact, commit-pinned failure
statement. `69/158` is a historical count across unknown commit ranges, not a
current reliability rate.

### 1.6 Two wording corrections

- Documentary: **"48 of 67 in-scope releases were absent from the current
  retained catalogue"** — not "never entered the system." The catalogue prunes.
- Secrets: **"No secrets were detected by the configured gitleaks rules; both
  reported findings were manually reviewed and classified as false positives"**
  — not "no API keys are exposed."

---

## 2. Where I would add to the review, not dispute it

**The rename gate is MANDATORY BY DEFAULT — reclassified in rev 2.1.** Rev 2
framed it as a substance change Jesse might object to. Round 2 corrected that
framing and I accept it: the gate is **a safety precondition to the supervised
run Jesse already authorised**, not a new product decision and not an optional
recommendation. His intent — one watched run, nothing unattended — is unchanged;
the gate defines the minimum evidence required before that intent can be carried
out safely.

Default, not negotiable by me:
- no real library file until the gate passes;
- one sacrificial, backed-up file after it passes;
- explicit stop conditions;
- no unattended expansion.

**Jesse as arbiter may explicitly waive any gate.** A waiver is recorded as
deliberate risk acceptance naming the missing evidence and the possible
consequence. **I will not offer a waiver as a peer alternative** merely because
the original wording said "soon."

**Fail-closed readiness is the more important half of §6.** Fixing the address
without changing the grading behaviour would produce a gate that works today and
silently fails open the next time the endpoint is unreachable. Both, or neither.

**Structural failure detection generalises beyond documentaries.** A scraper
that gets an unexpected template and returns zero must not report a successful
empty scan. That exact shape hid full-disc releases for months and would have
hidden a documentary crawl built without checking. This should be a property of
the scraper layer, not a documentary-specific check.

---

## 3. Execution order — rev 2.1: serial preservation, then parallel tracks

RSS diagnosis and rename forensics are largely independent and are no longer
expressed as one queue.

### Serial first — preservation and precedence (blocks everything)

1. Rename execution and real-file testing stay **frozen**.
2. Snapshot the RSS shadow evidence as **one atomic bundle**: database copy
   **plus WAL state**, qualification output, branch and commit, active
   configuration, container image digest, service version, collection
   timestamp, cycle range included, and a **checksum manifest**. Partial or
   mutually uninterpretable snapshots are worthless.
3. Repair the precedence chain. *(done in rev 2.1 — banners now at the top of
   both superseded files, contradictory authority wording struck.)*

**Do not delay step 2.** RSS evidence is time-dependent and degrades. After
preservation, rename work carries the higher consequence and takes priority,
because it can affect real files while RSS guards a metric and a future
decision.

### Track A — RSS evidence and qualification

**A1. Unique-miss analysis.** Per distinct actionable missed canonical URL:
title, media type, quality/category, actionable status, first and last miss
timestamps, usable cycles missed, first provable RSS observation, recovery
latency, whether it appeared in a normal feed, whether only in catch-up,
whether canonicalisation differed, whether it remained in the listing after RSS
acquired it, and final classification.

**Preserve the existing three-state rule.** Still in `listing_only` = missing;
later observed in `feed_only` = provably acquired; absent from both = **ambiguous,
not resolved**. Disappearance is not resolution.

**A2. Per-cycle metrics.** Normal and catch-up entry counts by feed, newest and
oldest publication timestamp by feed, **observed feed depth in seconds**, poll
gap since the previous usable cycle, **coverage margin = feed depth − poll gap**,
listing counts by source, the three URL sets, **the count and proportion of
full-disc entries in each upstream normal feed** (this measures §1.1's indirect
path), HTTP/parser outcome, and cycle kind.

**A3. Causal classification.** Every unique miss lands in exactly one bucket:
publication lag · finite-window displacement · taxonomy omission · catch-up-only
coverage · identity mismatch · parser/transport failure · persistent upstream
omission · ambiguous with current evidence.

**A4. Suitability is more than URL discovery.** RSS-primary also requires that
candidates survive identity parsing, relevance classification, hydration,
duplicate/library comparison and safe action preparation. A URL arriving
eventually is insufficient if it cannot reach the same actionable decision the
listing path produced.

**A5. Predeclared promotion thresholds — written BEFORE the new window opens.**
Zero RED persistent actionable misses; zero PENDING at closure; zero AMBIGUOUS
at closure unless Jesse explicitly accepts the evidence limitation; no negative
coverage margin at ordinary cadence, or a proven catch-up that closes it;
accepted latency stated in hours; acceptable classification/hydration failure
rate stated in advance; minimum usable cycles and wall-clock duration;
restart-recovery evidence; and **no pass when application reconciliation is
unavailable**. Without these written first, the analysis can describe data but
cannot produce a disciplined verdict.

**A6.** Collector networking repair (§1.3) and **fail-closed** readiness —
both, or neither.

**A7.** Population symmetry (`#191`), then a **fresh** qualification window.
Pre-change and post-change cycles are never merged.

`#191` acceptance: full-disc candidates cannot enter classification, hydration
or actions; the exclusion is counted and observable; listing and RSS policy
reasons use the same canonical identity; **the change does not claim to increase
upstream feed depth**.

### Track B — Rename safety

**B1. Evidence-availability audit FIRST.** `rename_jobs` is a single mutable row
per job, not an event ledger. Bucket only by fields actually retained or
reliably reconstructable; mark everything else **unknown** rather than inferring
it; document what cannot be recovered.

*Probably recoverable:* current status, present path values, `move_method` where
populated, current error category, collision category, timestamps, package
grouping, and a weak same/cross-volume inference from current mappings.

*Not honestly recoverable without another source:* commit range per job, attempt
count, full transition history, definitive volume topology at failure time,
post-failure filesystem state, historical hash integrity. Search retained logs,
database backups and prior review artifacts for corroboration; **record the gap
where none exists.**

**B2.** Identify any currently reproducible failure path, commit-pinned.
**B3.** Deterministic old-fail/new-pass tests plus fault injection around
temporary copy, publication, source disposal, database commit, process crash,
and restart recovery.
**B4. Add a durable rename event ledger** before future testing: attempt UUID,
job id, application version and commit, operation method, source and
destination, filesystem/mount and volume identity, pre-operation existence/size/
hash, each state transition, publication result, source-disposal result,
database commit result, post-operation existence/size/hash, exception type and
normalised error code, restart-recovery outcome.
**B5.** Copy-only rehearsal on the real storage surfaces, hashes verified.
**B6.** Restart and recovery invariants.
**B7.** One sacrificial, backed-up real file — **only** after B1–B6 pass.

### Track C — Security controls (independent; may run in parallel)

Repository-wide, history-aware secret review explicitly covering **generated
artifacts, deployment scripts, workflow files, and container/environment
templates**; CI secret scanning on pushes and pull requests; a reviewed
allowlist with comments and ownership; externalise and rotate the plaintext
Gotify token; and **a documented response procedure for a true positive**.

### Track D — Independent product work (no measured surface touched)

TV resolution filter + 720p chip · surface the full-disc setting in the UI ·
HDR10+ labels (`#185`) · documentary design pass · `#192` doc correction.
Scraper structural-failure detection (`#184`-adjacent) belongs here and should
be a property of the **scraper layer**, not a documentary-specific check: an
unexpected template returning zero must raise an error, never a successful
empty scan.

---

## 3b. Superseded serial phases (rev 2, retained for traceability)

### Phase 1 — Freeze and preserve
1. Rename execution and real-file testing stay frozen.
2. Snapshot the RSS shadow database and qualification artifacts.
3. Record branches, commits, configuration, timestamps, cycle counts.
4. Reconcile the decision record to point at this document. *(done on merge)*

### Phase 2 — Correct the evidence model
5. Full-disc reasoning corrected. *(done — §1.1)*
6. "97 cumulative relevant-miss observations" adopted. *(done — §1.2)*
7. Distinct missed URLs, recurrence, first/last timestamps, later-appeared-in-RSS.
8. Diagnose the constant 100 against the five hypotheses.
9. Separate transient timing misses from persistent coverage misses.

### Phase 3 — Repair qualification infrastructure
10. Fix collector networking (§1.3).
11. Readiness reconciliation fails closed.
12. Separate infrastructure failure from product rollback conditions.
13. Verify notifications cannot announce a false pass.

### Phase 4 — Diagnose rename reliability
14. Bucket the historical failures by commit range, cause, volume topology,
    operation method, and post-failure state.
15. Identify any currently reproducible failure path, commit-pinned.
16. Deterministic regression tests plus fault injection.
17. Copy-only rehearsal on the real storage surfaces, hashes verified.
18. Restart and recovery invariants.

### Phase 5 — Complete security controls
19. Finish repository-wide, history-aware secret review.
20. CI secret scanning plus a reviewed allowlist with ownership.
21. Externalise and rotate the plaintext Gotify token.
22. Record Jesse's no-response decision as accepted risk. *(see §4)*

### Phase 6 — Implement semantic changes
23. RSS full-disc symmetry (`#191`).
24. Unexpected scraper structure treated as an error, not an empty result.
25. RSS instrumentation (`#184`).
26. Qualification and operational documentation (`#192`).

### Phase 7 — Requalify from a clean boundary
27. Fresh post-change RSS qualification window.
28. Never merge pre-change and post-change cycles.
29. Evaluate persistent unique misses, not cumulative observations.
30. Application readiness and database evidence must agree.

### Phase 8 — Controlled rename rollout
31. One sacrificial, backed-up real file, only after every earlier gate passes.
32. Confirm source, destination, hash, database state, Plex visibility, restart.
33. Expand gradually with explicit stop conditions.

**Unsequenced, independent of the above:** TV resolution filter + 720p chip,
surfacing the full-disc setting in the UI, HDR10+ labels (`#185`), documentary
design pass. None touch RSS evidence or rename safety. They can run in any gap.

---

## 4. Accepted risks, recorded

- **No response to the external sender.** Jesse's decision, made with the
  tradeoff stated: silence avoids confirming a live monitored address, at the
  cost of possibly antagonising a legitimate reporter into public disclosure.
  ChatGPT proposed a minimal one-way acknowledgement; Jesse declined. **Not
  reopened.**
- **Public topology retained** (production hostname, NAS server and share names,
  drive letters). Not credentials, but not harmless either — reclassified from
  "acceptable" to **low-to-moderate information exposure** supporting phishing
  or reconnaissance. Mitigation is prevention of the next one, not cleanup of
  this one.
- **No history rewrite** across ~703 commits. Proportionate given no secret was
  found in history.

---

## 5. What review round 3 should attack

1. **§1.1's indirect path is now accepted but unmeasured.** A2 adds
   "count and proportion of full-disc entries in each upstream normal feed" to
   quantify it. Is that the right measurement, or does proving displacement
   require reconstructing the feed window at each poll?
2. **A5 thresholds.** Are "zero PENDING, zero AMBIGUOUS at closure" achievable
   in practice, or so strict that no window can ever pass and the criterion
   becomes theatre?
3. **B1's honesty test.** I have committed to marking unrecoverable dimensions
   as unknown. Is there a risk the surviving buckets are so thin that the
   analysis cannot support *any* rename conclusion — and if so, does the event
   ledger (B4) become a hard prerequisite rather than an improvement?
4. **Track C/D parallelism.** I assert security and UI work touch no measured
   surface. Is that true of the UI work, given it ships in the same image and a
   deploy restarts the container mid-RSS-window?
5. **The priority rule.** After preservation I put rename ahead of RSS on
   consequence. Does that hold when RSS evidence continues to accumulate and
   degrade while rename work is frozen anyway?
