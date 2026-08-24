# Round 21 — provenance

What is actually running, versus what only exists on the branch. This file
exists because earlier rounds produced findings that assumed deployed code and
were reasoning about branch code, or the reverse.

---

## 1. The running container

```
container: scanhound
image:     scanhound:latest
started:   2026-08-23T12:21:58Z
```

There is no `.git-commit` marker in the image, so the running commit cannot be
read directly from the container. What **can** be established directly:

```
/app/backend/arms.py  ->  NOT PRESENT in the deployed image
```

**The entire arm-registry feature — round 19 and round 20 alike — is absent from
the running container.** The deployed code predates it. This is the single most
important fact for reviewing the migration: there is no deployed writer that
produces round-19 or round-20 shaped rows, so the only shapes that can exist in
the live ledger are the pre-round-19 two-part keys, which is exactly what §1 of
`03-evidence.md` shows.

## 2. The branch

```
branch: fix/round12-attestation-authority
head:   c4d9dc0   R21 exact-head review: close R21-3b/4/8/10a-d/11/12/13
        951ec06   R21-1/5/6/7: attribution is a STATE
        f7250ba   Round 21 peer-review package
        1f77a1d   M19-1/M19-2 -- the head your exact-head review read
```

The enclosed patch is `1f77a1d..c4d9dc0`, so it is exactly what changed since
the code you reviewed.

`1f77a1d` is **not** an ancestor of `origin/main`
(`git merge-base --is-ancestor` returns false). `origin/main` is at `3c3369d`.

The commit is local and **has not been pushed**. Pushing, merging, deploying and
enabling are Jesse's decisions alone.

## 3. The live ledger is frozen, and why

`max(last_seen_at)` in `listing_claims` is `2026-08-22T15:50:43Z`, while the
container restarted on 2026-08-23T12:21:58Z. So the container has been up for
roughly a day without writing a new listing claim.

I am reporting this as an observation, not a diagnosis — I have not established
*why*, and it is outside this review's scope. It is stated because it has two
consequences a reviewer should know:

- The 266-row figure is stable and will not move underneath any measurement in
  this package.
- There is no concurrent writer during migration work, which removes a hazard
  but also means the concurrency argument in `01-request.md` §C5 is theoretical
  rather than observed.

## 4. `parser_version = "select_posts/1"` — how that value was justified

The deployed writer has **no parser-version constant at all**. So for rows
already in the ledger there is nothing to read, and the value cannot be
"recovered" in any honest sense.

It is justified instead by **byte identity of the parser to commit `ef2fb188`**:
the parsing code that produced those rows is byte-identical to the code that
`select_posts/1` names on the branch. That is an argument from provenance, not a
recorded fact, and the migration audit labels every such row
`provenance_class='reconstructed'` so the distinction survives in the data.

If you think byte identity is too weak a basis, that is a legitimate finding and
I would rather hear it now.

## 5. Citation warning for anyone reading line numbers

Listing-source descriptors sit at **`scanner_service.py:730/732/734`** on the
**deployed** image, but at **`743/745/747`** on this branch, where those line
numbers instead hold the Adit-HD descriptors. An auditor following a
branch-relative citation against the deployed file would read Adit-HD lines and
could reasonably conclude the manifest was fabricated. Cite by symbol where
possible.

## 6. What was NOT done

- The live database was never written to. Every measurement used a
  `VACUUM INTO` copy pulled out of the container.
- No image was built, no container recreated, no image pruned. The rollback
  image `scanhound:rollback-20260822-121500` still holds the writer code, and
  `ef2fb188` remains the durable anchor.
- No `git checkout`, `git stash` or branch switch was used at any point.
  `docker-compose.yml` carries required uncommitted modifications (the DV ingest
  key and `127.0.0.1:9721:9721`); baseline comparisons were done by extracting
  `git show HEAD:<path>` into a scratch directory instead, so those
  modifications were never at risk.
