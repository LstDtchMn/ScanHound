# ScanHound plan — revision 2, AUTHORITATIVE

**Date:** 2026-07-31 · **Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
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

### 1.1 The full-disc theory is dead — state it plainly

> **Full-disc asymmetry is a real correctness problem for future RSS-primary
> operation, but it does not explain the current `listing_only` miss
> observations.**

The comparison is `listing_only = listing URLs − RSS URLs`, computed on the
listing result **after** policy filtering. Since the listing path excludes
full-disc and the RSS path ingests it, full-disc surfaces as `feed_only`. It
cannot produce `listing_only` misses. My earlier claim that full-disc was
inflating the count required a pre-exclusion listing set, which the code does
not build.

`#191` remains necessary. It is no longer offered as an explanation of anything.

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

**Adopt the rename gate wholesale.** ChatGPT's sequence — classify failures,
prove no unexplained source loss, deterministic old-fail/new-pass tests, fault
injection, copy-only rehearsal on the real storage surfaces, hash verification,
then one sacrificial backed-up file — is materially safer than Jesse's
"supervised manual run soon." Jesse's *intent* (a watched run, nothing
unattended) is preserved; the gate in front of it is much stronger. I am
flagging this as a change in substance so Jesse can object.

**Fail-closed readiness is the more important half of §6.** Fixing the address
without changing the grading behaviour would produce a gate that works today and
silently fails open the next time the endpoint is unreachable. Both, or neither.

**Structural failure detection generalises beyond documentaries.** A scraper
that gets an unexpected template and returns zero must not report a successful
empty scan. That exact shape hid full-disc releases for months and would have
hidden a documentary crawl built without checking. This should be a property of
the scraper layer, not a documentary-specific check.

---

## 3. Execution order (ChatGPT's phases, adopted)

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

## 5. What review round 2 should attack

1. **Is §1.1 now stated correctly**, or have I over-corrected — is there any
   path by which full-disc contaminates `listing_only` that neither of us has
   considered?
2. **Phase 2 step 7** — is the distinct-URL analysis list sufficient to
   distinguish "RSS is fixable" from "RSS is structurally unsuitable"?
3. **Phase 4** — is bucketing historical rename failures actually achievable
   from the data retained, or am I promising an analysis the records cannot
   support? If the commit range per job was never recorded, step 14 may be
   undeliverable as written.
4. **§2 first paragraph** — I strengthened Jesse's rename decision beyond what
   he asked for. Is that the right call, or should the gate be presented to him
   as a choice rather than folded in?
5. **Phase ordering** — Phases 2 and 4 are independent. Should rename diagnosis
   run first, given it guards real files while RSS guards only a metric?
