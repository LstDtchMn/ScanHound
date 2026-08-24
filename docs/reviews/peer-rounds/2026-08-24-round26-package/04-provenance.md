# Round 26 — provenance

What is actually running, versus what only exists on the branch. Every figure
here was re-measured on 2026-08-24 for this package; none is carried forward
from an earlier round.

---

## 1. The running container

```
container : scanhound
image     : scanhound:latest
started   : 2026-08-23T12:21:58Z   (up ~33 hours)
```

```
/app/backend/arms.py  ->  ls: cannot access: No such file or directory
```

**The entire arm-registry feature — rounds 19 through 26 alike — is still absent
from the running container.** The deployed code predates it. Nothing in this
package is running anywhere. There is no deployed writer producing round-19 or
later shaped rows, so the only shapes the live ledger can contain are the
pre-round-19 two-part keys, which is what §2 confirms.

## 2. The live ledger

Read directly from `/dbvol/crawler.db` inside the running container:

```
rows in listing_claims : 266
distinct arm_key       : 3
max(last_seen_at)      : 2026-08-22T15:50:43.046582+00:00
```

Three distinct two-part keys, consistent with the three shipped hdencode feeds
and with the `legacy_migration_plan` mapping asserted in
`TestTheExactLiveKeyMappingIsAsserted`.

## 3. The "frozen ledger" — diagnosed, and it is not an incident

Carried as an undiagnosed observation through rounds 24 and 25. Diagnosed here.

The conclusion is that **the ledger was never being written in production, and
nothing is broken.** The evidence, in order:

**The crawler is healthy.** All three feeds crawl roughly hourly, continuously
through the window in question:

```
[15:34:13] Crawling 4K Movies... / Remux Movies... / TV Packs...
[16:38:14] Crawling 4K Movies... / Remux Movies... / TV Packs...
[17:35:47] Crawling 4K Movies... / Remux Movies... / TV Packs...
```

The only warnings in 24 hours are two `ReadTimeoutError` navigation retries,
both of which recovered on attempt 1/3.

**The deployed code contains no ledger writer at all**, with positive controls
so the zeros can be trusted:

```
  def get_connection        : 1     <- control: the grep works
  def __init__              : 1     <- control
  file size                 : 361300 bytes
  def record_listing_claims : 0
  listing_claims (any ref)  : 0     <- not the writer, not the table, nothing
```

**And that is correct, not a regression.** `origin/main` at `3c3369d` has a
`backend/database.py` of **exactly 361,300 bytes — byte-identical in size to the
deployed file** — and also contains zero references to `listing_claims`.
`git log -S "def record_listing_claims" origin/main` returns **no commits**: the
feature has never landed on main. The image (built 2026-08-22T17:10:25Z) is
faithfully built from main. My first reading of these facts was that a build had
dropped a live feature; checking main rather than assuming is what ruled that
out.

**So where did 266 rows come from?** They were written between
`2026-08-21T13:54:13Z` and `2026-08-22T15:50:43Z` — before the current image
existed. The only code that can write them is branch code. They are residue from
branch code run manually against the **live** database during development, and
they stopped when that stopped.

Three consequences worth stating plainly:

- The 266-row figure is stable and cannot move underneath a measurement. That is
  a property of the data being inert, not of a healthy steady state.
- **No positive control exists for "the writer works in production."** Every
  claim in this package about writer behaviour is measured in a test container,
  and no amount of review can substitute for that, because the code is unmerged.
- Development code wrote into the live database. The table is inert on main —
  nothing there reads or writes it — so this is not corrupting anything. It is
  still worth Jesse knowing, and it is his call whether those rows stay.

## 4. The branch

```
branch      : fix/round12-attestation-authority
head        : 3cbefdb   Round 25 peer-review package
              e26c2f7   -- the head your Round-25 review read
origin/main : 3c3369d
```

`git merge-base --is-ancestor HEAD origin/main` → **false**. The branch is not
on `main`.

The branch **is** pushed: `origin/fix/round12-attestation-authority` is at
`3cbefdb`, so you can read the code directly rather than only through the patch.

The round-26 changes are two commits on top of `3cbefdb`:

```
a9b53ed  Round 26 follow-up: the R25-1 refusal was inert, and two real baselines
3d75680  Round 26: five Round-25 findings, the seventh overstated A, regression G
```

`02-code-changes.patch` is `e26c2f7..a9b53ed`, scoped to `backend/`, `tests/` and
the retired-test mapping — 966 insertions across 5 files. Verified rather than
assumed: extracted `e26c2f7` into a clean tree, applied the patch, and confirmed
every touched file is byte-identical to this head once line endings are
normalised. So you can read either the patch or the pushed branch and get the
same code.

### Correction carried forward

The round-25 provenance file stated `e26c2f7` "has not been pushed". That was
true when written and is no longer. Rather than edit the sentence away, I struck
it through in place and dated the correction — a provenance file whose past
statements get silently rewritten cannot be used to check anything. See
`docs/reviews/peer-rounds/2026-08-24-round25-package/04-provenance.md` §2.

## 5. The branch is 11 commits behind main, and main has a failing test

Neither of these is a round-26 finding. Both turned up while establishing an
honest suite baseline, and both matter more to the eventual merge than anything
in this package.

### 5a. The gap

```
git merge-base --is-ancestor origin/main HEAD  ->  false
commits on main not in the branch : 11
commits on the branch not in main : 53
```

The 11 are a single coherent body of work — the JDownloader Click'n'Load
transport:

```
3c3369d Click'n'Load: gate the fallback on server_mode -- it was reaching a
        REAL JDownloader from unit tests
6a2524e jd-direct-connect-check: stream output, and drop the -w flag
05b84b2 Add jd-direct-connect-check.py
0b45b8e Design: quota exhaustion is not a verification hold
5c0d270 Add the N=25 discriminating test for the Turnstile gate
c2632a0 UI: show an unconfirmed hand-off as unconfirmed
af3a127 JD: fall back to the LOCAL JDownloader when the cloud send fails
64824bf Click'n'Load transport: a local hand-off that needs no cloud
b2efb61 Add jd-network-forensics.ps1
47fafc5 JD: raise myjdapi's 3-second request timeout
c0d4398 Retry cards: link to the source page, name the release, show the codes
```

They touch `backend/clicknload.py` (new, 142 lines), `backend/config.py` (+25)
and `backend/download_service.py` (+182). This branch touches none of those
files, so the merge should be clean — but "should be" is exactly the assumption
that has cost this project three losses in a single merge before. The rule
learned then applies here: enumerate what EACH side adds, never resolve by
taking a side.

### 5b. Main's own suite is not green

Baseline measured for this package (§6): `origin/main` at `3c3369d` fails one
test **of its own**:

```
tests/test_config.py::TestDefaultConfig::test_default_config_has_no_unexpected_keys
  AssertionError: Unexpected keys in _DEFAULT_CONFIG:
      {'jd_api_timeout_seconds', 'jd_clicknload_fallback', 'jd_clicknload_url'}
```

`tests/test_config.py` enforces a strict allow-list, `EXPECTED_DEFAULT_KEYS`.
Commits `47fafc5` and `af3a127` added three keys to `_DEFAULT_CONFIG` without
declaring them there. The established practice is visible in the history —
`704ebd2 "Declare the new config key in the expected-keys allowlist"` — so this
is a missed step, not a disputed design.

It is a real defect on main, small and self-contained, and it is **not** mine to
fix inside a review branch. It is raised separately for Jesse.

Two things follow for reading this package's numbers:

- The branch currently **passes** that test only because it lacks the feature
  that introduced the keys. That is an absence, not a fix, and it will surface
  the moment main merges in.
- A branch-vs-main failure comparison is therefore not a like-for-like measure
  of this work. §6 states both numbers rather than a single delta.

## 6. The suite, both sides, same method

Both runs use the same procedure in the same session: the tree extracted with
`git archive` (which cannot disturb the working tree), copied whole into a fresh
container from the same `scanhound:latest` image, with the same pinned test
dependencies (`pytest 9.1.1`, `pytest-asyncio 1.4.0`, `httpx 0.28.1`) and
bytecode caches cleared.

```
origin/main  3c3369d   809 files    1 failed, 5356 passed, 4 skipped   13:54
this branch  3d75680+  900 files    0 failed, 5769 passed, 4 skipped   16:04
```

Main's single failure is its own, described in §5b. **The branch has no
failures.**

The +413 passing tests are this branch's own additions across rounds 19–26; the
count is not comparable as a quality measure and is stated only so the two
numbers are not mistaken for like-for-like.

### Why the earlier figures in this session were wrong, twice

Both errors are mine and neither is a code problem, but a package that quotes a
number should say how the number was got.

**First**, the "11 pre-existing failures" carried in earlier packages was partly
fictional — see `03-evidence.md` §1. Two of the three test files it named do not
exist anywhere in the repository.

**Second**, my first attempt to re-measure produced **77** failures on main. That
was an instrument fault: I copied only `backend/` and `tests/` into the
container, so every test reading a repository file failed on its absence —

```
FileNotFoundError: 'docs/kometa/version_badges.yml'
```

— which is precisely what the standing rule *"copy the WHOLE tree for suite runs;
partial copies invent failures"* exists to prevent, and I broke it. The same
trap caught me a second time an hour later, when a container holding main's
`arms.py` beside the branch's `database.py` produced 21 phantom failures that
vanished on a full resync.

The figures above are from complete trees. The correction is recorded rather
than quietly replaced, because a suite number with no method attached is the
thing that made the original 11 durable for so long.

## 7. Authority

Pushing, merging, deploying, marking ready and enabling are Jesse's decisions
alone. Nothing in this package has been merged or deployed, and no reviewer
guidance can authorize those steps.
