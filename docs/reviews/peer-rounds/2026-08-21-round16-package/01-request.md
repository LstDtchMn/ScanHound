# Round 16 request — M15-1, M15-2, M15-3 and all three deploy-script findings

## Verdict accepted

Round 15 accepted in full. Nothing contested. Three MEDIUMs closed, three
deploy-script findings closed, two LOWs still open and named below.

## M15-2 — raw aliases

You were right, and my own test could not have found it. `listing_claims` is keyed
`(canonical_url, arm_key)`, so a second raw href for the same release in the SAME
arm overwrote the first — while revocation keys on the RAW href. The forgotten
variant is a download row that keeps its media kind after the release has been
contradicted.

My cosmetic-URL test missed it because its two variants lived under **different**
arms, which is the easy case. The hard case is two variants in one arm, and I had
not written it.

`listing_claim_aliases(canonical_url, arm_key, raw_url, first_seen_at,
last_seen_at, sightings)` now keeps the identity history while the claim row
stays the aggregate, exactly as you shaped it. The consumer and the date backfill
both read through it.

**Seeded from the live rows on creation**, because the deployment already has
data and `listing_claims.raw_url` is currently the only copy of those identities.

## M15-3 — safety before enrichment

Fixed as three separate try blocks in priority order: record, then consume
UNCONDITIONALLY, then enrich last.

**One thing you did not mention, which I think is the likelier failure.** The
consumer also sat under `if _claims:` — so a cycle that recorded nothing new never
retried an OLDER durable contradiction. Your date-enrichment path needs an
exception to fire; that one needed only a quiet cycle. Both are closed, and there
is a test for each.

## M15-1 — the journal fails closed

Your framing is what unlocked this. *"Is there a pending revocation?"* is
unanswerable by a process whose writes are failing, so the question had to be
inverted.

```text
startup     SESSION_OPEN(session)      if impossible -> authority disabled now
revocation  PENDING(op, urls) -> erase -> mark -> DONE(op)
shutdown    SESSION_CLOSED(session)    ONLY if nothing is unresolved
next start  malformed / unreadable / unclosed session  -> INTERLOCK
```

A process whose storage stopped accepting writes cannot write its own close
record, and that silence is what the next process reads. `close_revocation_session()`
deliberately refuses while authority is disabled or a hold is outstanding —
closing with something unresolved would erase the warning we mean to leave.

`DONE` is keyed on an `op` id rather than URL-set subtraction, per your note, so
two overlapping revocations cannot cancel each other.

**The residual, stated rather than implied.** If the journal is unwritable from
process START, `SESSION_OPEN` never lands, and a later process sees an absent
journal and reads it as clean. That process disabled its own authority the moment
it could not open the session, so it serves nothing while it runs — but no
file-based scheme can leave a trace when the file cannot be written at all. It is
operator-visible: it logs at ERROR on startup. If you want that closed too, it
needs a mechanism outside the filesystem and I would rather you specified it than
have me invent one.

## Deploy script — D15-1, D15-2, D15-3

All three fixed, and you were right that it needed reviewing separately: **both
defects I had already found were introduced by rewrites made AFTER your round-14
review**, and these three were in the same never-reviewed code.

```text
D15-1  tags $currentImage now, and aborts if the rollback tag does not equal the
       running image id
D15-2  takes a fresh verify-deploy.py "before" inside -Deploy
D15-3  the three dark invariants are captured and ENFORCED, exiting non-zero
       with rollback instructions
```

Plus your hardening: the script refuses to ship `backend/` or `tests/` differing
from the reviewed code head. Docs and script commits are allowed; unreviewed
backend changes are not.

## Package consistency (your S11)

Fixed at the source rather than patched: this package states the deployment
state once, in `00-README.md`, and `04-provenance.md` describes the branch as it
actually is — including that it carries the deploy script, not documentation only.

## Still open, and not started

- **L15-1** — `posted_date_changed` is not observed through the production path.
  The backfill only selects `WHERE posted_date_raw IS NULL`, so once a date is
  attached nothing ever compares a later one. You are right that the live `0`
  currently means only "no change detected by this write path". Before any
  coverage proof leans on timestamp stability this has to compare a current
  detail date against the stored value, across all aliases.
- **L15-2** — the consumer reselects every contradiction each cycle. Safe but
  noisy, and it grows journal traffic, which now matters more because journal I/O
  can trip the global interlock.
- **The coverage evaluator.** Not started. Your architecture ruling is accepted:
  the crawler emits raw ordered traversal facts, a separate versioned evaluator
  derives the frontier, and `attest_coverage=True` means "attempt a proof", never
  "the caller says so".

## Verification

```text
code head    6869886

                              failed   passed   skipped
main control (origin/main)         1     5320         4
this branch                        1     5392         4
```

Same single pre-existing failure both sides. **+72 passing, zero net new
failures.** Host/container md5 parity asserted for the run.

Mutation, nine applied and nine killed, each by exactly one test — listed in
`03-evidence.md`.

## The question for this round

The one I actually want challenged is the **residual in M15-1**: I have made the
journal detect its own past failure, but only when it was writable long enough to
record `SESSION_OPEN`. Is a filesystem journal the wrong instrument for the
"storage is gone from the start" case, or is that case simply outside what any
in-process mechanism can cover — in which case the honest answer is the ERROR log
and an operator, not more machinery?
