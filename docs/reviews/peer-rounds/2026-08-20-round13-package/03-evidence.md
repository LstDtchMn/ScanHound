# Evidence — every command, and what it returned

All runs are in throwaway containers from `scanhound:latest` with the code tree
copied in, never the 9p bind mount. Host/container md5 parity was asserted before
each run (see `04-provenance.md`).

## 1. Reproduction BEFORE the fix

The regression was written first and run against unmodified `main @ 6ac5cd2`.

```bash
docker exec -w /app sh-r12 python -m pytest \
  tests/test_round12_attestation_authority.py -q
```

```text
4 failed, 1 passed

FAILED ...::test_a_crawl_with_the_tv_arm_switched_off_must_not_attest
FAILED ...::test_an_early_stopped_crawl_must_not_attest
FAILED ...::test_a_crawl_with_page_errors_must_not_attest
FAILED ...::test_a_cancelled_crawl_must_not_attest

E   AssertionError: assert '4k' is None
E    +  where '4k' = get_scan_category('https://hdencode.example/...')
```

The 5th test passed and had to: a partial crawl must still be able to RECORD a
conflict. That asymmetry is the load-bearing idea in the fix — discovering a
contradiction is a positive observation any crawl can make; certifying the
absence of one is not.

These drive the real `BackgroundScanner.scan_once()` decision. The pre-existing
tests called `db.attest_scan_categories([URL])` directly, which presupposes the
entitlement instead of testing who may claim it.

## 2. Targeted, after the fix

```text
13 passed        tests/test_round12_attestation_authority.py
```

Includes the fault-injection case driving the real `scan_once()` sequence with
the atomic revocation raising, and the positive controls.

## 3. Full suite, against a like-for-like control

Two containers, provisioned identically in the same session, differing only in
the code tree under test.

```bash
docker exec -w /app <ctr> sh -c "python -m pytest -q --tb=no > /tmp/out.txt 2>&1"
```

```text
                              failed   passed   skipped   duration
main control (origin/main)         1     5320         4   804s
this branch                        1     5333         4   806s
```

The single failure is identical on both sides and pre-existing:

```text
FAILED tests/test_dv_settings.py::test_all_frontend_editable_settings_keys_are_in_model
```

**+13 passing, zero net new failures.** The 13 are the new test file.

## 4. Mutation results — both directions

Applied by copying a mutation script into the container and running it as a
file. (A heredoc via `docker exec` without `-i` silently applies nothing and
exits 0, which makes a mutant look like it survived; the mutated file's md5 was
checked against the host before each run.)

```text
mutation                                    killed   direction
crawl_attestation_verdict -> always True         5   over-permissive
crawl_attestation_verdict -> always False        2   over-strict
attest guard back to key PRESENCE                1   permanent de-attestation
rescan drops category_attested                   1   M12-3
failed revocation not retained                   1   M12-2
```

**The over-strict row is the one that matters most.** Every negative test in this
round asserts that something is NOT attested, so deleting attestation entirely —
or any gate that never fires — would satisfy all of them. The two positive
controls are what separate "correctly stricter" from "permanently broken".

## 5. A figure I got wrong first, and how it was caught

My initial full-suite run reported **74 failed**. That was the instrument, not
the code: the container had no `docs/` or `scripts/`, and

```text
tests/test_version_labeler.py:222   reads docs/kometa/version_badges.yml
tests/test_verification_hold.py     reaches into scripts/
```

Running `origin/main` through the identical method reproduced the same **74**,
which is what identified it as an artifact rather than a regression. Both
containers were then given `docs/` and `scripts/`, and both runs dropped to 1.

The figures in section 3 are the post-correction ones. The 74 is recorded here
rather than quietly replaced, because a partial code copy inventing failures is a
recurring failure mode in this project and the correction is part of the evidence.

## 6. Deployment state

```bash
docker exec scanhound sh -c "grep -c 'media_kind' /app/backend/database.py"
```

```text
0        # and 0 for crawl_attestation_verdict
```

The running container predates all of the media-kind work. **Nothing in this
package, and nothing from rounds 10-12, is deployed.**
