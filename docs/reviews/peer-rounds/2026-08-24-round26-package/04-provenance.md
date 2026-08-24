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

The round-26 changes described in this package are committed on top of `3cbefdb`
— see the commit named in `03-evidence.md` §8 and the enclosed
`02-code-changes.patch`, which is scoped to `backend/`, `tests/` and the
retired-test mapping.

### Correction carried forward

The round-25 provenance file stated `e26c2f7` "has not been pushed". That was
true when written and is no longer. Rather than edit the sentence away, I struck
it through in place and dated the correction — a provenance file whose past
statements get silently rewritten cannot be used to check anything. See
`docs/reviews/peer-rounds/2026-08-24-round25-package/04-provenance.md` §2.

## 5. Authority

Pushing, merging, deploying, marking ready and enabling are Jesse's decisions
alone. Nothing in this package has been merged or deployed, and no reviewer
guidance can authorize those steps.
