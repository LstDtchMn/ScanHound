# Provenance

## SHAs

```text
repository   LstDtchMn/ScanHound
branch       fix/round12-attestation-authority

code head    1aff26f949a54466c322a38b53798389ee4831fd
base         6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
```

`1aff26f` is the last commit touching `backend/` or `tests/`. Later commits are
this package only.

## Commits added since the round-18 package

```text
f700185   R19  scope every part of a proof to the claim it supports
                 M18-2  duplicates scoped per arm
                 M18-3  pages sealed after enumeration; positions 1..N
                 M18-4  CoverageEvidenceSnapshot
                 M18-1  contracts keyed (arm_key, parser_version)

1aff26f   R19  one arm identity, and the ledger moved to it
                 M18-1  backend/arms.py; producer stamps the key; ledger
                        stores it; 209 live rows migrated without guessing
```

Everything before `2cfdbc0` is unchanged from the round-18 package and its
provenance still stands, including the nine self-repairing commits listed there.

## Blob hashes at the code head

```text
backend/arms.py                    8fb710c3690b   NEW
backend/coverage.py                596e067c1c6f
backend/scanner_service.py         20db6c170cd7
backend/database.py                af7d043d4c33
backend/background_scanner.py      cfce19e17380
```

## Self-repair on this branch, round 19 only

None. Both commits this round are remediation of findings you raised, not
repairs of code I wrote earlier on this branch.

Three defects were caught before commit and appear only here, because a diff
cannot show them:

- **A partial arm registry would have resolved `ddlbase:remux` cleanly.**
  I first wired the migration to build its registry from `_build_sources()`,
  which returns only the feeds selected for the current scan. With just the
  2160p remux feed selected, the ambiguous key looks unambiguous and the rows
  get an attribution that may be wrong. Caught by writing the ambiguity test
  before the wiring, then noticing the wiring could not satisfy it. The fix is
  `KNOWN_ARMS` plus a drift test in both directions.

- **A producer test that could not run.** The first version of the
  same-category claim test used the two DDLBase feeds. The DDLBase parser
  cannot read the HDEncode fixture markup, so it recorded zero claims and the
  test failed for a reason unrelated to the defect. Reported rather than
  quietly retargeted: a test that fails because its premise is wrong looks
  identical to one that found a bug.

- **A mid-enumeration failure injected at the wrong call.**
  `canonicalize_listing_url` is called twice per post, so failing on call 2
  aborted before the first sighting was appended and the page had zero
  sightings — which would have made "the partial sightings are kept" pass for
  the wrong reason if I had asserted `>= 0`.

## Test-container identity

Two throwaway containers, both provisioned from `scanhound:latest` with
`backend/`, `tests/`, `pytest.ini`, `main.py`, `docs/` and `scripts/` copied in,
plus `pytest`, `pytest-asyncio` and `httpx`.

```text
sh-r12    the branch at 1aff26f
sh-main   origin/main at 6ac5cd2, exported with `git archive`
```

`sh-main` was verified to contain **no** `backend/coverage.py` and **no**
`backend/arms.py` before running, and `backend/database.py` md5 was asserted
equal between the exported tree and the container.

`docs/` and `scripts/` matter: without them the suite reports ~74 failures,
diagnosed in round 13 by reproducing the same 74 on the `main` control.

Both full-suite runs were executed **concurrently in this session**, so the
durations are inflated by CPU contention and are not comparable to earlier
rounds. Pass and fail counts are unaffected.

One earlier branch run was **discarded**: I copied new backend files into
`sh-r12` while it was mid-run, which invalidates it. Recorded because a
discarded run and a run that never happened are not the same thing.

## What is NOT covered

- **No CI attestation.** Builder-side runs, described so they can be re-run.
- **Nothing is persisted.** The evaluator still returns objects; no proof, page
  or sighting is stored. Gate items 6 and 9 are untouched.
- **No versioned required-arm policy.** `covers_release()` still takes the set
  explicitly. Gate item 5.
- **No caller sets `attest_coverage=True`**, nothing writes `category_attested`,
  and `ORDERING_CONTRACTS` is still empty and still enforced in code.
- **The M17-1 defeat is still documented, not solved.** Two terminal outliers
  produce a January-2024 frontier; what changed in round 17 is that such a
  frontier is marked non-authoritative. Solving it needs a source-observable
  invariant that does not exist.
- **The migration has never run against the real database.** It is tested
  against seeded copies of the live shape (`hdencode:4k` / `:remux` / `:tv`) and
  against the ambiguous `ddlbase:remux`, but the deployed container is still the
  round-14 code and no migration has executed on `/dbvol/crawler.db`.
- **The live figures describe the ROUND-14 container**, not this code head.
