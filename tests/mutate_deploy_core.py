"""Do the Docker fixture cases actually catch the defects they were written for?

Twenty-five passing cases prove nothing on their own. A guard written beside the
code it checks passes BY CONSTRUCTION -- the only evidence that a case is load
bearing is that it FAILS when the defect it exists to catch is put back.

The count is also not the point; tests/test_deploy_core_docker.ps1 prints an
invariant-to-evidence table for that, and its NOT MODELLED rows are the honest
part. This file only answers the narrower question: of the invariants that ARE
modelled, which cases would notice if the defect came back?

Each mutant below reintroduces one reviewed defect and names the case that must
fail. A mutant that SURVIVES is reported as a survivor, not quietly dropped and
not papered over by weakening the case.

Anchors are matched exactly once and the line numbers of the change are
printed, because a substring anchor that matches in two places silently mutates
the wrong site.

Run:  python tests/mutate_deploy_core.py
      python tests/mutate_deploy_core.py --only SR3-5 --only SR3-6
      python tests/mutate_deploy_core.py --recover-only

Every mutant runs the entire real-Docker suite, so a full pass takes hours
rather than minutes. That cost is the point: a mutant that is not run against
every case cannot be said to have been caught by the case named for it. --only
selects mutants by substring of their label, for iterating on ONE new guard; a
run that used --only says so in its verdict, because a filtered pass is not
evidence about the mutants it skipped.
"""
import io
import os
import re
import subprocess
import sys
import time

CORE = "scripts/deploy-core.ps1"
SUITE = "tests/test_deploy_core_docker.ps1"
# CORE is the file that deploys production. Restoring it in a `finally` covers
# an exception; it does NOT cover this process being killed, and on 2026-08-26
# that is exactly what happened -- the run was terminated mid-mutant and left
# `if ($false)` sitting in the host-storage guard on disk. Nothing in the repo
# would have said so.
#
# So the pre-mutation text is written to a sibling file BEFORE any mutation and
# removed only on a clean exit. A leftover backup is therefore proof that the
# previous run died with a mutant applied, and the next run repairs it before
# reading anything.
BACKUP = "tests/.deploy-core.premutation"


def recover_from_killed_run():
    if not os.path.exists(BACKUP):
        return
    good = io.open(BACKUP, encoding="utf-8", newline="").read()
    cur = io.open(CORE, encoding="utf-8", newline="").read()
    if cur != good:
        io.open(CORE, "w", encoding="utf-8", newline="").write(good)
        print("!! %s still held a MUTANT from a killed run. Restored from %s." % (CORE, BACKUP))
    else:
        print("   a backup from a previous run was found; %s was already intact." % CORE)
    os.remove(BACKUP)


# One command an operator (or the next run) can use to repair the deploy engine
# after a killed run, without paying for a full mutation pass:
#     python tests/mutate_deploy_core.py --recover-only
if "--recover-only" in sys.argv:
    recover_from_killed_run()
    sys.exit(0)

recover_from_killed_run()
ORIG = io.open(CORE, encoding="utf-8", newline="").read()
io.open(BACKUP, "w", encoding="utf-8", newline="").write(ORIG)

# Unknown arguments are REFUSED, not shrugged off. This checker rewrites
# scripts/deploy-core.ps1 IN PLACE for hours; running the full destructive pass
# because someone typed --help (which happened, this session) is the worst
# possible reading of a typo.
_KNOWN_FLAGS = {"--only", "--recover-only"}
_argv = sys.argv[1:]
_i = 0
while _i < len(_argv):
    a = _argv[_i]
    if a == "--only":
        _i += 2  # value consumed
        continue
    if a in _KNOWN_FLAGS:
        _i += 1
        continue
    sys.stderr.write(
        "unknown argument %r -- refusing to run. This tool mutates\n"
        "scripts/deploy-core.ps1 on disk for hours; it does not guess.\n"
        "known: --only <substring>, --recover-only\n" % a)
    sys.exit(2)

MUTANTS = [
    (
        "OPS-1: build from the mutable primary worktree instead of the clean one",
        """        $b = Invoke-Native { docker build -t $candidate -f (Join-Path $src 'Dockerfile') $src }""",
        """        $b = Invoke-Native { docker build -t $candidate -f (Join-Path $cfg.Repo 'Dockerfile') $cfg.Repo }""",
        # Exactly what round 2 did: the context is whatever is on disk, so
        # untracked and git-ignored local files enter the image.
        ["CASE D"],
    ),
    (
        "OPS-2: promote the recovery tag as soon as the build succeeds",
        """        Good "$($cfg.ImageTag) is untouched by the build -- the candidate is quarantined\"""",
        """        Require-Native { docker tag $candidate $cfg.ImageTag } "early promote" | Out-Null""",
        # The unverified candidate is now reachable by the scheduled recovery
        # task's `up --no-build --pull never`.
        ["CASE B"],
    ),
    (
        "SR2-1: drop the drift check that runs after target resolution",
        """        Assert-ComposeAgrees -Pinned $cfg.PinnedCompose -TargetCompose $targetCompose `
                             -ProjectDir $cfg.Repo -When 'after target resolution'""",
        """        # mutant: no drift check here""",
        # The second check still fires, but only AFTER a ten-minute build --
        # which is the round-2 ordering defect, and what CASE E pins.
        ["CASE E"],
    ),
    (
        "artifact identity: accept whatever image the container ends up on",
        """        if ($script:D.new_image_id -ne $script:D.built_image_id) {
            Stop-Deploy ("the running container is on image $($script:D.new_image_id) but the build produced $($script:D.built_image_id). It is running something else.")
        }""",
        """        # mutant: no image identity check""",
        ["CASE C"],
    ),
    (
        "OPS-5: do not observe production after destructive work",
        """            try { $script:D.observed = Observe-CurrentContainerState -Cfg $cfg } catch { }""",
        """            $script:D.observed = $null""",
        ["CASE F"],
    ),
    (
        "OPS-4: key the port assertion by HOST port, as it was before the fixture found it",
        # Two lines, because a 12-space anchor is a SUBSTRING of the
        # 16-space copy of the same line in Observe-CurrentContainerState --
        # the harness's exactly-once rule caught that, which is the whole
        # reason it exists.
        """            $ports = $pj.Text | ConvertFrom-Json
            $key = "$($Cfg.ContainerPort)/tcp\"""",
        """            $ports = $pj.Text | ConvertFrom-Json
            $key = "$($Cfg.PortNum)/tcp\"""",
        # Production maps 9721->9721 so this reads correct there forever. The
        # fixture publishes host->8080 and exposes it immediately.
        ["SEED"],
    ),
    (
        "OPS-2 in-flight race: take no mutex, so recovery can recreate mid-deploy",
        """        $mutex = New-Object System.Threading.Mutex($false, $cfg.MutexName)""",
        """        $mutex = New-Object System.Threading.Mutex($false, ($cfg.MutexName + '-mutant-unshared'))""",
        # A lock nobody else holds is not a lock. CASE G contends for the real
        # name from the test process; if the engine asks for a different one it
        # sails straight through.
        ["CASE G"],
    ),
    (
        "OPS-4: accept any /health answer, rather than asserting status=ok",
        # Re-anchored 2026-08-28: the health check became a POLL (da91e6a) and
        # the old single-shot anchor matched 0 times -- the checker refused to
        # count that, which is correct, and this is the repair. The mutant now
        # blinds the poll's verdict branch; $healthDone = $true is outside the
        # anchor so the loop still terminates and the suite stays finite.
        """                if ($h.status -eq 'ok') { Good "${Phase}: /health status=ok (after ${waited}s)" }
                else { $problems += "${Phase}: /health answered but status=$($h.status), not ok" }""",
        '''                Good "${Phase}: /health answered"''',
        ["CASE I"],
    ),
    (
        "build transport: ignore the build's exit code",
        """        if ($b.ExitCode -ne 0) {
            @($b.Output) | Select-Object -Last 15 | ForEach-Object { Say "    $_" }
            Stop-Deploy "the BUILD failed (exit $($b.ExitCode)). The old container is untouched, and $($cfg.ImageTag) still points at the last known-good image."
        }""",
        """        # mutant: build exit code ignored""",
        # EVIDENCE-1. This used to be credited to CASE A, which catches it only
        # by its refusal STRING -- the deploy stopped anyway, one guard later,
        # because the candidate image did not exist. Candidate tags are
        # deterministic by SHA, so that accident evaporates the moment a
        # previous run left one behind. CASE A2 seeds exactly that state, and
        # under this mutant it does not merely lose a diagnostic: the deploy
        # activates a STALE image and reports VERIFIED. The credit is therefore
        # A2 alone, so a passing run means the SAFETY outcome was caught and
        # not the wording.
        ["CASE A2"],
    ),
    # ---- SR3-4: the build guard must describe what the engine builds ------
    (
        "SR3-4: accept a build context that is not the root the engine builds",
        """    if ($ctxFull -ne $rootFull) {""",
        """    if ($false) {""",
        # The round-3 guard exactly: `context: ./subdir` is ACCEPTED and the
        # returned context is then ignored, so the engine builds the root and
        # the ledger reports the target commit's provenance for a tree compose
        # would never have built.
        ["a build section this engine"],
    ),
    (
        "SR3-4: accept a dockerfile override the engine does not honour",
        """    if ($df -cne 'Dockerfile') {""",
        """    if ($false) {""",
        # The other half, and a different lie: the right TREE built with the
        # wrong RECIPE. `dockerfile: Dockerfile.production` is accepted while
        # the engine builds the default Dockerfile.
        ["a build section this engine"],
    ),
    (
        "SR3-4: inspect only the build section, so a service-level platform: sails through",
        """    if ($svcExtra.Count -gt 0) {""",
        """    if ($false) {""",
        # The guard's FIRST version exactly, and the reason it needed a second:
        # `docker compose config --format json` renders platform as a SIBLING
        # of build -- measured on this host, service keys build/command/
        # entrypoint/image/networks/platform against build keys context/
        # dockerfile -- so a check that walked $svc.build never saw it, while
        # `docker compose build --print` resolves the same file to
        # target.app.platforms. Compose and this engine then build DIFFERENT
        # images under one provenance claim.
        ["platform"],
    ),
    # ---- SR3-5: deploy-vs-deploy serialization ---------------------------
    (
        "SR3-5: take a deploy-instance lock nobody else can hold",
        """        $deployMutex = New-Object System.Threading.Mutex($false, $cfg.DeployMutexName)""",
        """        $deployMutex = New-Object System.Threading.Mutex($false, ($cfg.DeployMutexName + '-mutant-unshared'))""",
        # A lock nobody else holds is not a lock. Deliberately the same shape
        # as the recovery-mutex mutant above, and it must be caught by a
        # DIFFERENT case -- SR3-5 contends the deploy lock, CASE G contends the
        # recovery lock, and neither may cover for the other.
        ["a second deploy refuses"],
    ),
    (
        "SR3-5: release the deploy-instance lock on the line after taking it",
        """        Good "holding the deploy-instance lock $($cfg.DeployMutexName) -- no second deploy can start\"""",
        """        Good "holding the deploy-instance lock $($cfg.DeployMutexName) -- no second deploy can start"
        $deployMutex.ReleaseMutex(); $haveDeployLock = $false""",
        # The verifier's mutant. Section 1 still reads identically -- the lock
        # is asked for, taken, and reported -- and the whole suite still came
        # back 23 passed / 0 failed, because the refusal case above holds the
        # lock from the TEST side and so can never observe when the ENGINE lets
        # go. The "still HELD" case is the one that can, and it must be the one
        # that fails here: a kill credited to the refusal case would mean the
        # row is still over-claiming.
        # "still HELD" alone would ALSO match the OPS-2 case ("the recovery
        # tag still held the PREVIOUS image"), and a mutant credited to the
        # wrong case is a survivor wearing a kill's clothes.
        ["still HELD late in the run"],
    ),
    # ---- SR3-6: rollback guidance ----------------------------------------
    (
        "SR3-6: drive the rollback offer from new_container_id again",
        """    $observedId = "$($Ledger.observed.container_id)\"""",
        """    $observedId = "$($Ledger.new_container_id)\"""",
        # The finding itself. Compose partially replaces the container and
        # returns nonzero, section 6 never runs, new_container_id stays null --
        # and the operator is not offered the rollback for the unverified
        # container the observer can see running.
        ["SR3-6"],
    ),
    # ---- SR3-7: -WhatIf --------------------------------------------------
    (
        "SR3-7: let -WhatIf fall through into the build and the deploy",
        """            $script:D.verdict = 'plan only'
            return (New-Result $cfg)""",
        """            # mutant: -WhatIf no longer stops before the build""",
        # -WhatIf is documented as making no build, no tag, no container
        # recreation and no production mutation. If that sentence is not
        # enforced it is a claim, not a contract.
        ["SR3-7"],
    ),
    # ---- SR3-1: storage identity -----------------------------------------
    (
        "SR3-1: activate against host sources whose identity failed",
        """            if ($hp.Code -ne 0) {""",
        """            if ($false) {""",
        # The probe still runs and its result is still recorded; the deploy
        # simply proceeds anyway. That is the pre-SR3-1 engine with extra
        # logging -- which is exactly why the case asserts the CONTAINER was
        # not replaced, not merely that something was printed.
        ["SR3-1: a host source"],
    ),
    (
        "SR3-1: treat a host probe that could not RUN as a pass",
        """            if ($hp.Reason -ne 'probed') {""",
        """            if ($false) {""",
        # NOT the decoy-source case: there the probe runs fine and reports a
        # failure, so this line is never reached. The case that reaches it is
        # the one where the probe container itself cannot start.
        ["cannot be obtained"],
    ),
    (
        "SR3-1: never probe the container's bind mounts at all",
        """    $nas = $null
    if ($NasSpec) {""",
        """    $nas = $null
    if ($false) {""",
        # The reviewer's blocker in its purest form: compose exited 0, the
        # image is right, the port is bound, /health says ok -- and nobody
        # asked where /library/tv actually points.
        ["SR3-1: a container bind"],
    ),
    (
        "SR3-1: run the container probe but ignore its verdict",
        """    } elseif ($r.Code -ne 0) {""",
        """    } elseif ($false) {""",
        # Distinct from the mutant above: the measurement happens, the
        # judgement does not. A ledger full of probe output and a green verdict
        # is worse than no probe, because it reads like proof.
        ["SR3-1: a critical destination"],
    ),
    (
        "SR3-1: treat a container probe that could not RUN as a pass",
        """    if ($r.Reason -ne 'probed') {""",
        """    if ($false) {""",
        # "Could not measure" collapsing into "fine" is the shape the
        # 2026-07-26 outage took: LastTaskResult 0, nine shares unmounted.
        ["SR3-2: a post-reconcile"],
    ),
    # ---- SR3-2: the final container --------------------------------------
    (
        "SR3-2: qualify the candidate, then declare VERIFIED without rechecking",
        """        $c2 = Invoke-RuntimeChecks -Cfg $cfg -NasSpec $nasSpec -Phase 'final'""",
        """        $c2 = @{ Problems = @(); Unknown = @(); ContainerId = $script:D.new_container_id; NasReason = 'n/a'; NasCode = 'n/a' }""",
        # Round 3 exactly: container-inspect success plus image-id equality,
        # then VERIFIED. The image is right and the instance is dead.
        ["SR3-2: a post-reconcile"],
    ),
    (
        "SR3-2: report the candidate container as the one the final checks saw",
        """        $script:D.final_checks_container_id = $c2.ContainerId""",
        """        $script:D.final_checks_container_id = $script:D.candidate_container_id""",
        # The checks still run, but the ledger names the wrong container, so an
        # operator reading VERIFIED cannot tell which container was qualified.
        # A proof nobody can attribute is not a proof.
        ["SR3-2: the reconcile recreates"],
    ),
]


ONLY = [sys.argv[i + 1] for i, a in enumerate(sys.argv) if a == "--only" and i + 1 < len(sys.argv)]


def run_suite():
    t0 = time.time()
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", SUITE],
        capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    failed = re.findall(r"^\s*FAILED:\s*(.+)$", out, re.M)
    return r.returncode, [f.strip() for f in failed], out, time.time() - t0


def line_of(text, anchor):
    return text[:text.index(anchor)].count("\n") + 1


print("=" * 78)
print("CONTROL  unmutated -- every case must pass")
print("=" * 78)
code, failed, out, secs = run_suite()
print("  exit=%d  failures=%d  (%.0fs)" % (code, len(failed), secs))
for f in failed:
    print("    still failing: %s" % f)
if code != 0:
    print("  CONTROL IS ALREADY FAILING; every mutant below would prove nothing.")
    os.remove(BACKUP)
    sys.exit(1)

ok = True
survivors = []
selected = [m for m in MUTANTS if not ONLY or any(o.lower() in m[0].lower() for o in ONLY)]
if ONLY:
    print()
    print("--only %s: running %d of %d mutants. A filtered pass is NOT evidence"
          % (", ".join(ONLY), len(selected), len(MUTANTS)))
    print("about the %d that were skipped." % (len(MUTANTS) - len(selected)))
    if not selected:
        print("  --only matched NOTHING; nothing was mutated.")
        os.remove(BACKUP)
        sys.exit(1)
for label, old, new, expect in selected:
    print()
    print("=" * 78)
    print("MUTANT  %s" % label)
    print("=" * 78)
    body = ORIG.replace("\r\n", "\n")
    n = body.count(old)
    if n != 1:
        print("  ANCHOR MATCHED %d TIMES -- skipped, proves nothing" % n)
        ok = False
        continue
    print("  patching %s line %d" % (CORE, line_of(body, old)))
    io.open(CORE, "w", encoding="utf-8", newline="").write(body.replace(old, new))
    try:
        code, failed, out, secs = run_suite()
    finally:
        io.open(CORE, "w", encoding="utf-8", newline="").write(ORIG)

    caught = [f for f in failed if any(w.lower() in f.lower() for w in expect)]
    for f in failed:
        print("    FAILED: %s" % f[:92])
    print("  exit=%d  total failures=%d  expected-catcher failed=%s  (%.0fs)"
          % (code, len(failed), bool(caught), secs))
    if code != 0 and caught:
        print("  --> KILLED by %s" % ", ".join(expect))
    else:
        print("  --> SURVIVED: %s did not fail. The case does not pin this defect."
              % ", ".join(expect))
        survivors.append((label, expect))
        ok = False

io.open(CORE, "w", encoding="utf-8", newline="").write(ORIG)
os.remove(BACKUP)
print()
print("=" * 78)
scope = ("%d of %d mutants (--only %s)" % (len(selected), len(MUTANTS), ", ".join(ONLY))
         if ONLY else "all %d mutants" % len(MUTANTS))
if ok:
    print("VERDICT: %s KILLED -- every defect run here is caught by the case written for it" % scope)
    if ONLY:
        print("         This was a FILTERED run. It says nothing about the mutants it skipped.")
else:
    print("VERDICT: %d of %s SURVIVED -- those cases are not load bearing:" % (len(survivors), scope))
    for label, expect in survivors:
        print("   %-64s expected %s" % (label[:64], ", ".join(expect)))
print("  %s restored" % CORE)
sys.exit(0 if ok else 1)
