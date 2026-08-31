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

Only one invocation may run at a time: it rewrites scripts/deploy-core.ps1 in
place, and two runs sharing that file corrupt each other's backup and each
other's results. A second run refuses to start while tests/.deploy-core.mutation-lock
is held by a live process; a lock left by a DEAD one is taken over, because the
run that repairs a killed pass IS the next run.

Every mutant runs the entire real-Docker suite, so a full pass takes hours
rather than minutes. That cost is the point: a mutant that is not run against
every case cannot be said to have been caught by the case named for it. --only
selects mutants by substring of their label, for iterating on ONE new guard; a
run that used --only says so in its verdict, because a filtered pass is not
evidence about the mutants it skipped.
"""
import atexit
import glob
import io
import os
import re
import socket
import subprocess
import sys
import time

CORE = "scripts/deploy-core.ps1"
# R5-101-1. The finding is that the deploy engine's transaction had an
# AUTOMATIC CONSUMER that was blind to it, so half the new guards live in the
# recovery task rather than the engine. A checker that could only mutate the
# engine could not ask whether those guards are load bearing, which is the one
# question this file exists to answer.
#
# Kept as a SEPARATE prefix rather than one glob over both. The recovery step
# below restores a file from whatever backup it finds, and a single shared
# prefix would let a mount-nas-shares backup be written into
# scripts/deploy-core.ps1 -- the file that deploys production. The two never
# share a name.
MOUNT = "scripts/mount-nas-shares.ps1"
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
# R4-101-2, and this was OBSERVED, not theorised. The backup path used to be
# one fixed name shared by every invocation, and there was no lock. A second run
# started while the first was mid-pass therefore:
#   * DELETED the first run's backup in its own recovery step (below), so the
#     first run's `os.remove(BACKUP)` at the end raised FileNotFoundError -- a
#     pass whose control had 0 failures and whose only mutant was KILLED exited
#     1, reported as a FAILURE;
#   * and voided BOTH runs' mutant-left-on-disk protection, because after that
#     deletion no file on disk said scripts/deploy-core.ps1 -- the file that
#     deploys production -- was currently holding a mutant.
#
# So: one lock, and a backup path unique per run.
BACKUP_PREFIX = "tests/.deploy-core.premutation"
MOUNT_BACKUP_PREFIX = "tests/.mount-nas-shares.premutation"
_STAMP = "%d-%d" % (os.getpid(), int(time.time()))
BACKUP = "%s-%s" % (BACKUP_PREFIX, _STAMP)
MOUNT_BACKUP = "%s-%s" % (MOUNT_BACKUP_PREFIX, _STAMP)
# path -> (its own backup prefix, this run's backup file). One entry per file
# this tool is allowed to rewrite; the prefixes are disjoint so a backup can
# never be restored into the wrong file.
TARGETS = {
    CORE:  (BACKUP_PREFIX, BACKUP),
    MOUNT: (MOUNT_BACKUP_PREFIX, MOUNT_BACKUP),
}
LOCK = "tests/.deploy-core.mutation-lock"


def _pid_alive(pid):
    """Is that process still running? Conservative: 'cannot tell' means YES.

    Never os.kill(pid, 0) on Windows -- os.kill there routes through
    TerminateProcess and would KILL the process it is asking about.
    """
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return bool(ok) and code.value == 259        # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return True


def _release_lock():
    # Read, CLOSE, then remove. Windows refuses to unlink a file that is still
    # open, so removing it from inside the `with` raised PermissionError, the
    # except below swallowed it, and every run left its lock on disk -- which
    # the next run then had to break as "held by a DEAD run". Found by the
    # concurrency harness, not by reading this.
    try:
        if not os.path.exists(LOCK):
            return
        with io.open(LOCK, encoding="utf-8") as fh:
            held = fh.read()
        if ("pid=%d" % os.getpid()) in held:
            os.remove(LOCK)
    except Exception:
        pass


def acquire_lock():
    """Refuse to start while another invocation holds the lock.

    A lock whose holder is DEAD is taken over rather than left to wedge the
    tool: the run that repairs a killed pass is the next run, so refusing on a
    stale lock would mean the one command that fixes scripts/deploy-core.ps1
    could never start.
    """
    if os.path.exists(LOCK):
        try:
            held = io.open(LOCK, encoding="utf-8").read().strip()
        except Exception:
            held = "(unreadable)"
        pid = None
        for tok in held.replace("\n", " ").split():
            if tok.startswith("pid="):
                pid = tok[4:]
        if pid is not None and _pid_alive(pid):
            sys.stderr.write(
                "another mutation run is already in progress -- refusing to start.\n"
                "  lock: %s\n  held by: %s\n"
                "This tool rewrites %s in place for hours; two runs sharing it\n"
                "corrupt each other's backup and each other's results.\n"
                "If that process is really gone, delete the lock file above.\n"
                % (LOCK, held, CORE))
            sys.exit(2)
        print("!! the lock at %s was held by a DEAD run (%s); taking it over." % (LOCK, held))
        os.remove(LOCK)
    d = os.path.dirname(LOCK)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, ("pid=%d host=%s started=%s\n"
                  % (os.getpid(), socket.gethostname(),
                     time.strftime("%Y-%m-%dT%H:%M:%S"))).encode("utf-8"))
    os.close(fd)
    atexit.register(_release_lock)


def recover_from_killed_run():
    """Repair every target from whatever backup a killed run left behind.

    Runs UNDER the lock, so at most one backup per file can be in flight; more
    than one means a leftover from before this protection existed, and that is
    said out loud rather than picked between silently.

    Each file is matched to its OWN prefix. A single glob over both would be
    able to write a mount-nas-shares backup into scripts/deploy-core.ps1.
    """
    for path, (prefix, mine) in sorted(TARGETS.items()):
        left = sorted(glob.glob(prefix + "*"), key=os.path.getmtime)
        left = [p for p in left if p != mine]
        if not left:
            continue
        if len(left) > 1:
            print("!! %d pre-mutation backups of %s were left on disk: %s"
                  % (len(left), path, ", ".join(left)))
            print("   restoring from the most recent and removing the rest.")
        src = left[-1]
        good = io.open(src, encoding="utf-8", newline="").read()
        cur = io.open(path, encoding="utf-8", newline="").read()
        if cur != good:
            io.open(path, "w", encoding="utf-8", newline="").write(good)
            print("!! %s still held a MUTANT from a killed run. Restored from %s." % (path, src))
        else:
            print("   a backup from a previous run was found; %s was already intact." % path)
        for p in left:
            os.remove(p)


def drop_backup():
    for path, (_prefix, mine) in sorted(TARGETS.items()):
        try:
            if os.path.exists(mine):
                os.remove(mine)
        except OSError as e:
            # Never turn a clean pass into a failure over the bookkeeping file.
            # The shared-path defect surfaced as exactly this exception at exit.
            print("   (could not remove %s: %s)" % (mine, e))


# One command an operator (or the next run) can use to repair the deploy engine
# after a killed run, without paying for a full mutation pass:
#     python tests/mutate_deploy_core.py --recover-only
if "--recover-only" in sys.argv:
    acquire_lock()
    recover_from_killed_run()
    sys.exit(0)

acquire_lock()
recover_from_killed_run()
ORIG = {}
for _p, (_prefix, _mine) in sorted(TARGETS.items()):
    ORIG[_p] = io.open(_p, encoding="utf-8", newline="").read()
    io.open(_mine, "w", encoding="utf-8", newline="").write(ORIG[_p])


def restore_all():
    for p, text in ORIG.items():
        io.open(p, "w", encoding="utf-8", newline="").write(text)

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
            Stop-Deploy "the BUILD failed (exit $($b.ExitCode)). The old container is untouched, and $($cfg.ImageTag) still points at the PRIOR image it named before this run."
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
    # ---- R4-101-1: the promotion transaction -----------------------------
    (
        "R4-101-1: leave the promotion standing when the final qualification fails",
        """            Invoke-PromotionRevert -Cfg $cfg -Why 'the FINAL container failed its instance-level qualification'""",
        """            # mutant: the promotion is left standing after the final qualification fails""",
        # THE reviewer's defect, exactly as round 3 shipped it and exactly as
        # round 3 argued for it. The verdict is still NOT VERIFIED and the
        # ledger still says so -- what changes is that the recovery namespace
        # keeps pointing at the image this run just failed to qualify, so the
        # recovery recreate the runbook calls a rollback re-creates the
        # candidate instead of restoring the prior image. The case must catch
        # it by the OUTCOME: it re-runs the recovery recipe and asserts which
        # image comes back.
        ["R4-101-1: a FINAL-qualification failure"],
    ),
    (
        "R4-101-1: revert the tag but keep calling it promoted",
        """            $script:D.promoted        = $false""",
        """            # mutant: the tag was reverted but the ledger still calls it promoted""",
        # A quieter version of the same harm, and the reason `promoted` had to
        # become the CURRENT state of the tag rather than a history flag. The
        # image really is restored -- and Test-RollbackAdvisable reads
        # `promoted`, so the wrapper would suppress the one-command rollback
        # while the pinned recipe genuinely would restore the prior image. The
        # operator is denied the rollback in a state where it works.
        ["R4-101-1: a FINAL-qualification failure"],
    ),
    (
        "R4-101-1: claim a rollback exists on a first-ever deploy",
        """            $script:D.promotion_state = 'promoted; NO PRIOR IMAGE existed to restore'""",
        """            $script:D.promotion_state = 'promoted, then REVERTED to the prior image'""",
        # Round 3's runbook sentence, in the ledger: "the old image is still on
        # disk and the recovery task knows only that image" is a description of
        # an image that does not exist when nothing was ever deployed. Nothing
        # about the tag changes under this mutant -- only the claim made about
        # it -- so the case has to assert the WORDING as well as the state.
        ["a FIRST-EVER deploy"],
    ),
    (
        "R4-101-1: a nonzero storage probe code is not a storage failure",
        """        if ("$($phase.Code)" -ne '0') { return $true }""",
        """        # mutant: a nonzero probe code is not read as a storage failure""",
        # The wrapper's recreate CREATES a container and Docker resolves bind
        # sources at container-create time. Under this mutant a run that failed
        # because /library/tv was not the share it claimed to be is handed the
        # plain recreate with no warning -- which binds the TV rename
        # destination to whatever that path currently is.
        ["the recreate is not recommended after a storage failure"],
    ),
    (
        "R4-101-1: a storage probe that could not RUN is not a storage failure",
        """        if ($reason -ne 'probed') { return $true }""",
        """        # mutant: an unrunnable probe is not read as a storage failure""",
        # The other half, and the shape the 2026-07-26 outage actually took:
        # the measurement never happened and the absence read as a clean
        # result. UNKNOWN is not proven, and it is emphatically not a licence
        # to recreate against unproven sources.
        ["the recreate is not recommended after a storage failure"],
    ),
    (
        "R4-101-2: a dead final container's probe is still a storage failure",
        """        if ($reason -eq 'not-running' -and $phase.Running -is [bool] -and -not $phase.Running) { continue }""",
        """        # mutant: a probe that could not ENTER a dead container is a storage failure""",
        # The finding. Promotion needs zero problems and zero unknowns at the
        # candidate phase and the host proof is a Stop-Deploy gate, so EVERY
        # post-promotion failure with the probes on reads host 'probed / 0',
        # candidate 'probed / 0', final 'not-running'. Under this mutant that
        # -- the most likely NOT VERIFIED shape -- prints a STORAGE alarm over
        # two passing source proofs and demotes the real rollback to step two.
        ["after a dead final container", "the recreate is not recommended"],
    ),
    (
        "R4-101-2: exempt ANY unrunnable probe once the container is dead",
        """        if ($reason -eq 'not-running' -and $phase.Running -is [bool] -and -not $phase.Running) { continue }""",
        """        if ($reason -ne 'probed' -and $phase.Running -is [bool] -and -not $phase.Running) { continue }""",
        # The other direction, and the one that would quietly undo SR3-1: a
        # probe that TIMED OUT against a container that later died is still
        # UNKNOWN, and UNKNOWN is not proven. Only 'not-running' is a
        # consequence of the container being dead.
        ["the recreate is not recommended"],
    ),
    (
        "R4-101-2: reword the REVERT-FAILED promotion_state",
        """            $script:D.promotion_state = 'promoted; the REVERT FAILED'
            $script:D.problems += (""",
        """            $script:D.promotion_state = 'promoted; the REVERT DID NOT TAKE'
            $script:D.problems += (""",
        # The cross-file contract S3 named. scripts/merge-and-deploy.ps1 keys
        # its reddest paragraph on -like '*REVERT FAILED*'; reworded, this
        # state falls through to the "this should not be reachable, report it"
        # block and the operator is never given the `docker tag` that repoints
        # the recovery namespace. NO other case notices: SR3-2 only asserts
        # -notlike '*REVERT FAILED*', and SR3-6 builds its ledgers by hand.
        ["every promotion_state the engine writes"],
    ),
    (
        "R4-101-2: say the revert restores the last VERIFIED image",
        """                  "The recovery recipe now restores that PRIOR image again -- the image " +""",
        """                  "The recovery recipe now restores the last VERIFIED image again -- " +""",
        # S2. What is restored is recovery_tag_before, the tag VALUE when the
        # build started. On the live host that is a hand-built image this
        # engine never qualified, and a prior run ending 'the REVERT FAILED'
        # leaves an unqualified one there too.
        ["names a VERIFIED image"],
    ),
    (
        # Re-anchored for R5-101-1: establishment became a GATE, so the call is
        # no longer a bare statement and the old anchor matched 0 times. The
        # mutant is unchanged in meaning -- no record is written at all -- and
        # is deliberately distinct from the fail-OPEN mutant below, which still
        # writes one and then ignores whether it worked.
        "R4-101-2: do not journal the promotion before moving the tag",
        """        if (-not (Write-PromotionJournal -Cfg $cfg -Prior $script:D.recovery_tag_before -Candidate $built)) {
            Stop-Deploy ("the promotion journal could not be established at $($cfg.PromotionJournal) " +
                         "($($script:D.promotion_journal)). $($cfg.ImageTag) has NOT been moved and still " +
                         "names the PRIOR image. Moving it without a durable record of the prior tag is the " +
                         "state R5-101-1 exists to remove: a run killed in that window leaves the recovery " +
                         "task recreating production onto an image nothing qualified, with nothing on disk " +
                         "saying what to restore.")
        }""",
        """        # mutant: no journal is written before the tag moves""",
        # S4. Without it, a run killed between the tag move and the revert
        # leaves scanhound:latest on an unqualified image with NO ledger and
        # nothing on disk -- and mount-nas-shares.ps1 catches
        # AbandonedMutexException and proceeds, so recovery activates it.
        ["the promotion journal is OPEN"],
    ),
    (
        "R4-101-2: leave the promotion journal open after a completed revert",
        """            $script:D.promotion_state = 'promoted, then REVERTED to the prior image'
            # The tag is settled and READ BACK, so the transaction really is
            # closed. Only here and after VERIFIED -- a REVERT FAILURE and the
            # NO PRIOR IMAGE case both leave an unqualified image in the
            # recovery namespace, and their journals stay on disk saying so.
            Clear-PromotionJournal -Cfg $Cfg""",
        """            $script:D.promotion_state = 'promoted, then REVERTED to the prior image'
            # mutant: the journal is left open after a completed revert""",
        # The other half. A journal that is never cleared makes every
        # subsequent run report an interrupted promotion that is not there,
        # which is how a real one stops being believed.
        ["the promotion journal is OPEN"],
    ),
    # ---- R5-101-1: the transaction and its automatic consumer -------------
    (
        "R5-101-1: journal establishment is fail-OPEN again -- warn, then move the tag anyway",
        """        if (-not (Write-PromotionJournal -Cfg $cfg -Prior $script:D.recovery_tag_before -Candidate $built)) {""",
        """        if ($false) {""",
        # R4-101-1a exactly as it shipped. Everything still runs -- the record
        # is still attempted, the ledger still says it could not be established
        # -- and the tag moves regardless, which is the reachable state with
        # latest = candidate and NO durable record of the prior tag at all.
        ["C2: a journal that cannot be established"],
    ),
    (
        "R5-101-1: report the interrupted transaction instead of repairing it",
        """            $norm = Invoke-PromotionJournalNormalize -Cfg $cfg -Tx $tx
            $script:D.journal_normalized = $norm.Result
            if (-not $norm.Ok) { Stop-Deploy $norm.Result }
            $noPriorBaseline = [bool]$norm.NoPrior
            Good "the interrupted transaction was resolved: $($norm.Result)\"""",
        """            # mutant: the interrupted transaction is reported and not repaired""",
        # R4-101-1b exactly as it shipped: the warning is still printed and
        # recovery_tag_before is then taken from the current mutable tag, which
        # after an interrupted run is the CANDIDATE. C3 is the only case that
        # asks what the rollback baseline became.
        ["C3: a stale journal is REPAIRED"],
    ),
    (
        "R5-101-1: at deploy startup, an unreadable record is treated as no record",
        """        if (Test-Path -LiteralPath $Cfg.PromotionJournal) {
            return [pscustomobject]@{ State = 'malformed'; Record = $null
                                      Why = "$($Cfg.PromotionJournal) exists but could not be read as a transaction record" }
        }""",
        """        # mutant: a present-but-unreadable record is reported as absent""",
        # Conservation of unknown, in the engine. The recovery task refuses to
        # recreate on a record it cannot read; if the engine calls the same file
        # "no transaction" the two consumers disagree about the same bytes, and
        # the engine is the one that then builds on an unknown baseline.
        ["C3b: a MALFORMED journal REFUSES the deploy"],
    ),
    (
        "R5-101-1: at deploy startup, accept a record naming a DIFFERENT image tag",
        """    elseif ("$($rec.image_tag)" -ne "$($Cfg.ImageTag)")      { $bad = "the record names image_tag '$($rec.image_tag)', not $($Cfg.ImageTag)" }""",
        """    # mutant: a record naming another tag is accepted as this deployment's transaction""",
        # The asymmetry that would put the two consumers at odds over the same
        # bytes. scripts/mount-nas-shares.ps1 refuses such a record; without
        # this check the engine would restore the FOREIGN record's prior image
        # onto its own tag, so a transaction nothing here opened would decide
        # what scanhound:latest points at.
        ["C3b: a MALFORMED journal REFUSES the deploy"],
    ),
    (
        "R5-101-1: clear the journal on every not-promoted exit, not only this run's own record",
        """        if ("$($script:D.promotion_journal)" -like 'open*') {
            Clear-PromotionJournal -Cfg $Cfg
        }""",
        """        Clear-PromotionJournal -Cfg $Cfg""",
        # The wider rule R4-101-2 shipped. It is right for a record THIS run
        # opened and destroys an inherited one: pre-flight deliberately carries
        # a no-prior record forward, because the tag still names an unqualified
        # image and that record is the only thing making the recovery task
        # refuse to recreate onto it. Under this mutant any build failure
        # deletes it.
        ["C5b: a carried-forward no-prior record"],
    ),
    (
        "R5-101-1: the recovery task recreates without consuming the transaction",
        """    $txApproved = Resolve-PromotionTransaction
    if ($txApproved -ne $true) {
        Fail ("The promotion transaction could not be resolved (the gate returned " +
              "'$txApproved' instead of an explicit approval). The container was NOT recreated: " +
              "the recipe names $RecoveryImageTag, and nothing here can say the image behind that " +
              "tag was ever qualified.") 9
    }""",
        """    # mutant: the recovery task recreates without consuming the transaction""",
        # THE finding, in the automatic consumer. Everything the deploy engine
        # does is unchanged -- the record is written atomically, verified, and
        # reported -- and scripts/mount-nas-shares.ps1 recreates production onto
        # scanhound:latest regardless of what that record says.
        ["C1: a deploy KILLED after provisional promotion"],
        MOUNT,
    ),
    (
        "R5-101-1: the recovery task treats an unreadable record as no transaction",
        """            return @{ State = 'malformed'; Record = $null; Why = "$PromotionJournal is not valid JSON" }""",
        """            return @{ State = 'none'; Record = $null; Why = 'mutant: unreadable is treated as absent' }""",
        # UNKNOWN resolved in favour of the current mutable tag, which is the
        # one direction this must never fail in: the tag may name an image
        # nothing qualified, and a torn write is exactly how a killed run leaves
        # a record that will not parse.
        ["C4: a MALFORMED journal"],
        MOUNT,
    ),
    (
        "R5-101-1: the recovery task accepts a record naming a DIFFERENT image tag",
        """        if ("$($rec.image_tag)" -ne $RecoveryImageTag) {""",
        """        if ($false) {""",
        # A file under C:\\ProgramData\\ScanHound\\deploy would then choose which
        # Docker tag an elevated Scheduled Task rewrites, and a record left by
        # some other transaction would be acted on as if it were this recipe's.
        ["C4: a MALFORMED journal"],
        MOUNT,
    ),
    (
        "R5-101-1: the recovery task treats an interrupted FIRST-EVER deploy as no transaction",
        """        if ((-not [bool]$rec.has_prior)) {
            return @{ State = 'no-prior'; Record = $rec; Why = "opened $($rec.opened_utc); that deploy found no previous $RecoveryImageTag" }
        }""",
        """        if ((-not [bool]$rec.has_prior)) {
            return @{ State = 'none'; Record = $rec; Why = 'mutant: a first-ever interrupted deploy is not a transaction' }
        }""",
        # The one valid record that is NOT restorable. Treating it as absent
        # recreates production onto the unqualified first-ever candidate, with
        # nothing to roll back to afterwards -- the worst state in the set, and
        # the one an "it's probably fine, the tag is current" reading produces.
        ["C5: an interrupted FIRST-EVER deploy"],
        MOUNT,
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
    drop_backup()
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
        drop_backup()
        sys.exit(1)
for mutant in selected:
    label, old, new, expect = mutant[0], mutant[1], mutant[2], mutant[3]
    target = mutant[4] if len(mutant) > 4 else CORE
    print()
    print("=" * 78)
    print("MUTANT  %s" % label)
    print("=" * 78)
    body = ORIG[target].replace("\r\n", "\n")
    n = body.count(old)
    if n != 1:
        print("  ANCHOR MATCHED %d TIMES -- skipped, proves nothing" % n)
        ok = False
        continue
    print("  patching %s line %d" % (target, line_of(body, old)))
    io.open(target, "w", encoding="utf-8", newline="").write(body.replace(old, new))
    try:
        code, failed, out, secs = run_suite()
    finally:
        restore_all()

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

restore_all()
drop_backup()
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
print("  restored: %s" % ", ".join(sorted(ORIG.keys())))
sys.exit(0 if ok else 1)
