# Scripts round 2 — all nine findings addressed, and one asymmetry I want named

**Branches:** `ops/deploy-and-permission-scripts` (`8ae7837`),
`fix/kometa-design-not-config` (`50652e6`, PR #99)
**Previous review:** 2026-08-25, verdict REQUEST CHANGES, nine findings

Every finding was verified against the code before being touched. **I refuted
none.** OPS-1, OPS-4's threshold arithmetic, OPS-6's revoke scope and KOM-1's
self-contradiction were all confirmed by reading the exact lines.

---

## Read this first: the rewrite reset the testing to zero

You wrote that two-thirds of the old deploy script had never executed. Both
scripts have now been **replaced rather than patched**, because the defects were
design-level. So the honest position is worse than before, not better:

| | old | now |
|---|---|---|
| deploy script sections executed | 2 of 5 | **0 of 7** |
| deploy script tests | 0 | **0** |
| permission script tests | 0 | **19 failure-injection** |

The permission script's destructive and undo paths are now exercised against
disposable fixtures and mutation-checked in both directions. **The deploy script
has nothing.** It is the one that rebuilds production, and it is the one with no
tests — that asymmetry is the single most important thing in this package.

I did not write deploy tests because faking the interesting states — a build
that fails while an old healthy container keeps running — needs real Docker
fixtures, and I would rather agree the shape with you than guess at it and
produce another set of guards that pass by construction.

## What each finding became

| ID | Response |
|---|---|
| **OPS-1** | Confirmed: line 243 said `git pull`, line 244 ran `git fetch`. Now fetch must succeed, tracked modifications refuse, the ref resolves to one SHA, the worktree is **actually** checked out, HEAD is confirmed, and every merged PR's commit must be an ancestor. |
| **OPS-2** | Build and activate are separate, both exit codes **required**. Artifact identity added: container ID must change AND running image must equal the image just built. |
| **OPS-3** | `Invoke-Native` returns the exit code. Structured `--json` with `bucket`; every check must be explicitly `pass` or `skipping`. The known-main-failure whitelist is **deleted**, not scoped. |
| **OPS-4** | PROBLEM vs UNKNOWN separated; unknown fails the run. Exact port via structured inspect. `/health` status asserted. Log check reworded as a window observation. |
| **OPS-5** | State ledger printed from a `finally`. `Die` no longer claims nothing was attempted. |
| **OPS-6** | `-Revoke` removes **all** script-owned rules regardless of grant flags, runs the same verifier, and says plainly it cannot revoke capability from other rules. |
| **OPS-7** | prepare → validate → commit → verify, via a sibling candidate file. |
| **KOM-1** | Renamed to `DV_BADGE_DESIGN.md`. Five live consumers updated. |
| **KOM-2** | Tests prove repo-owned invariants; external geometry is dated owner-observed evidence. |

## Where I want to be argued with

**1. Is `except`-free identity enough?** OPS-2's identity check is
"container ID changed AND running image == built image". I did not add the
`org.opencontainers.image.revision` label you suggested, because `docker compose
build` does not set it without a Dockerfile change, and I did not want to touch
the Dockerfile in a scripts commit. So the source SHA is proven only
transitively: the worktree was verified at the SHA, and the image was built from
that worktree. **Is that transitive chain sufficient, or does the SHA need to be
in the image?**

**2. The build/activate split changes behaviour.** `docker compose build` then
`up -d --no-build --force-recreate` is not identical to `up -d --build`. It
always recreates, where `up --build` may not. Safer for identity checking, but I
have not thought hard about the case where the build succeeds and the activate
fails — the new image exists and the old container is still running, which is a
state the old script could not produce.

**3. The checkout is destructive to the working tree.** OPS-1's fix runs
`git checkout --detach $target`, which moves whatever the operator had checked
out. It refuses on tracked modifications first, but it will silently move you
off your branch. Correct for a deploy script? Or should it refuse unless already
on the right ref?

**4. The BOM mutation taught me something about my own checker.** With
validate-before-commit, reverting to the BOM-writing call makes the script fail
**closed** — the live file never gets a BOM, so the no-BOM assertion passes
trivially and the *grant* fails instead. My mutation checker reported SURVIVED
until I corrected its expectation. Worth noting because "the test that would
catch X" and "the test that fails when X is reintroduced" are not the same test
once a validation gate exists.

## What I did not do

**No `fsync` on the permission candidate before the atomic move.** You did not
raise it there, but it is the same crash-consistency gap I already owe you on
the quarantine interlock. Stating it rather than letting you find it.

**Archives not rewritten.** `superpowers/plans`, `superpowers/specs` and
`reviews/peer-rounds` still describe `dv_badges.yml` as a deliverable. They are
point-in-time records; rewriting them would make the history lie. The tests
exclude them by path, which is a judgement you may disagree with.

**Neither script has been run for real.** Tonight's deploy was done by hand
before this rewrite existed.
