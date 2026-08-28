# Round 4 — three PRs, and the one that is about to touch production

**Base:** `main` @ `0a2751d`
**PRs:** #101 `ef45a04` (ready) · #94 `9ed50d6` (draft) · #100 `7ee50f0` (draft)
**Previous:** your Scripts Round 3, verdict REQUEST CHANGES on SR3-1 and SR3-2.
Both are closed here, along with SR3-3 through SR3-7 and EVIDENCE-1.

This is the largest package this sequence has sent you, and size dilutes
review quality. So the three PRs are ranked, and I am explicit about where I
want your attention and where I do not.

| PR | attention | why |
|---|---|---|
| **#101** deploy path | **the round** | 29 commits you have never seen, and it is about to run against production for the first time |
| **#94** hybrid sweep | **second** | a union merge across three scanner files — the risk is silent LOSS, not a bad addition |
| **#100** desktop retirement | **skim** | −13,404 lines of proven-dead code; low risk, but it is a public repo |

---

# PR #101 — the deploy path

## What closed since you last looked

**SR3-1 (storage identity).** You said the deploy path recreates ScanHound
without proving the NAS bind-mount invariant `mount-nas-shares.ps1` treats as
safety-critical, and that the shared mutex made it worse because recovery
cannot repair while deploy holds the lock. Both correct.

`scripts/nas-probe.ps1` keeps **no copy** of the identity rule. It parses
`mount-nas-shares.ps1` and lifts the `$probeScript` here-string out of it
verbatim, applying three declared substitutions so a fixture can retarget it.
Each anchor must match exactly once or the module refuses, and the anchors
contain `/library/tv`, `9p` and the UNC origin template — so editing any of
those three production constants fails loudly here instead of leaving a stale
copy. `mount-nas-shares.ps1` itself is **not modified**; it remains a live
Scheduled Task and this code only reads it.

Probes run before activation (host sources, holding the mutex), after
activation (container binds + the write/delete test on `/library/tv`), and
again after the reconcile. **A probe that could not RUN is UNKNOWN and
refuses** — never a pass.

**SR3-2 (final container).** The reconcile is now the FINAL ACTIVATION, not
cleanup. `Invoke-RuntimeChecks` is one function called twice — candidate, then
whatever the reconcile leaves running — looking the container up by name each
time and returning the id it actually observed. The three-minute log window is
deliberately not repeated.

**SR3-4/5/6/7 + EVIDENCE-1** are closed as you specified; SR3-3 is recorded as
a dated owner decision rather than an inherited default (below).

## What my own project review then found in that work

I ran a five-lane review of the whole project afterwards. It found things, and
two of them are the interesting kind.

**The health check would have failed the first real run.** `SettleSeconds = 15`
and a single `/health` probe — but production takes **~63 seconds** to answer:
the entrypoint's stale-lock cleanup runs to its full 60s cap before the app
starts. Measured on the 2026-08-26 deploy: StartedAt 21:59:55 → first answer
22:00:58. The fixture never caught it because the fixture app answers in ~2s.
Startup time is a production invariant no modelled state carried — your
"12/12 means all MODELLED states passed" point, demonstrated.

It is now a poll with a 120s ceiling that retries **only on no-answer**; a
served `degraded` is still an immediate failure, so CASE I keeps its exactness.

**Three guards added.** Equal deploy/recovery lock names now refuse (one config
edit would have silently reverted the build-outside-the-recovery-lock design
with no suite noticing). Both mutation checkers now reject unknown argv —
they previously ran the full destructive pass on `--help`, which is how a
fixture leak started. And `dv-scripts.yml` gained the NAS pin suite, the
missing path entries, and `ops/**` on the branch filter — the measured reason
this branch had **zero CI runs**.

## Evidence, measured on the owner's host at this head's parents

```
tests/test_deploy_core_docker.ps1     26 passed, 0 failed
tests/test_nas_probe_pin.ps1          14 passed, 0 failed, 1 skipped
                                      + live differential: lifted rule vs
                                      original agree byte-for-byte across all
                                      9 REAL NAS shares, incl. the /library/tv
                                      write+delete probe
tests/mutate_deploy_core.py           23 mutants: 22 killed in the full pass;
                                      the 23rd had a stale anchor after the
                                      health-poll edit, was re-anchored, and
                                      killed via --only
tests/test_claude_permissions_script.ps1   57 passed, 0 failed
CI on this branch                     dv-scripts: success — the first
                                      execution of these suites on any machine
                                      that is not the owner's
```

## Where I want you to push hardest

**1. Is the storage proof actually a proof?** The fixture cannot mount a 9p
share, so the fixture cases model the SHAPE of the failure (bind an ordinary
directory where a share is expected; bind the critical target read-only) while
the live differential covers the real thing but runs only on the owner's host
and only when asked. Is that split sound, or is there a state neither half
reaches?

**2. The probe-cannot-run path.** It refuses. But the probe runs a throwaway
container; if the daemon is degraded enough to fail that, is refusing the right
call, or does it turn a recoverable deploy into an outage?

**3. The health poll's terminal-on-any-answer rule.** I chose it so a served
`degraded` fails instantly rather than being retried into the deadline. Is
there a real startup sequence that answers with a transient non-ok status
before becoming healthy? If so I have traded a false negative for a different
one.

**4. Something I want challenged directly.** The full mutation pass came back
22 of 23, with the 23rd SKIPPED because my health-poll edit made its anchor
stale. I re-anchored it and killed it with a filtered run. Two runs, both
green, no single run that covers all 23 against the current code. I think that
is adequate; say so if you disagree.

**5. SR3-3, the auto-sync decision.** You said you would not infer consent from
a default whose safety rationale was false. Agreed. The order taken was: ship
the logging fix FIRST (the old log dropped `result["removed"]` entirely, so
"no removals in the log" meant nothing at all), observe, then decide. Nine
unattended passes since, `0 removed` in every one — 3,223 matched, 9 added on
the latest. The owner authorized it explicitly with those numbers in hand and
the decision is recorded in `backend/config.py` with its date and evidence.
Is four-to-nine passes a sufficient base, or is the sample still too thin?

---

# PR #94 — hybrid listing sweep, re-attested

49 commits behind main, three conflicting files (`scanner.py`,
`scanner_service.py`, `test_scanner_service.py` — `database.py` auto-merged).
Merged, resolved as unions, re-attested at the merged head.

**The risk here is silent LOSS, not a bad addition**, and it is the failure
mode we have been burned by before. So the resolution was checked by diffing
the merged result against BOTH parents line by line, hunting specifically for
anything either side had that the merge does not. Every apparently-missing line
was accounted for — `is_full_disc_title` and `_FULL_DISC_TITLE_RE` had **moved**
to `backend/release_policy.py`, not vanished; main's per-source CSS selectors
all survive verbatim; the `_post` dict is the claimed union of both sides'
fields.

```
harnesses at the merged head   R-1 33 passed · R-3 71/40/31 exit 0 · R-4 12
                               · R-5 14 · mutation 10/10 · selftest PASS
                               · SHA256SUMS 0 mismatches
full pytest at the head        6067 passed / 5 skipped / 0 failed
CI                             10 checks, all pass
```

**One behaviour change to weigh:** a zero-signal legacy cached row now REFUSES
instead of movie-matching. **Three of main's nine pins in
`test_scanner_carries_is_tv.py` were dropped** during the rewrite — argued as
structurally covered, but that argument is exactly the kind you have caught
before, so please check it rather than take it.

---

# PR #100 — retire the desktop stratum

−13,404 lines: `ui/` (QML), `main.py`, spec files, 8 dead config keys, and
three Settings toggles wired to nothing — including `exclude_720p`, a checkbox
a user could tick that filtered nothing. README and HELP rewritten around what
ScanHound actually is now.

Every deletion carries a proof-of-deadness (grep/import across `backend/`,
`frontend/`, `tests/`, `docs/`), independently re-derived. Five README claims
traced to code. Full pytest green.

**Skim-level ask:** it is a public repo, so tell me if the new README
over-claims anything, and if any deletion looks like it removes something a
future contributor would need.

---

# Evidence boundary

You can read all three PRs and their CI. You **cannot** execute the
Windows/PowerShell suites, the Docker fixture, or the live-NAS differential —
those numbers are author-reported, measured on the owner's host. Say which of
your conclusions depend on them.

The deploy script has **never run against production**. The one deploy since
the rewrite was by hand, using this engine's method — and doing it revealed
that the obvious `docker compose up --build` would have built the wrong branch,
because Compose resolves `build: .` against the project directory. That is why
the engine builds from a worktree with plain `docker build`.

No merge, deploy, permission change, or enablement is authorized by this
review. Those remain Jesse's alone.
