"""Do the Docker fixture cases actually catch the defects they were written for?

Eighteen passing cases prove nothing on their own. A guard written beside the code
it checks passes BY CONSTRUCTION -- the only evidence that a case is load
bearing is that it FAILS when the defect it exists to catch is put back.

Each mutant below reintroduces one reviewed defect and names the case that must
fail. A mutant that SURVIVES is reported as a survivor, not quietly dropped and
not papered over by weakening the case.

Anchors are matched exactly once and the line numbers of the change are
printed, because a substring anchor that matches in two places silently mutates
the wrong site.

Run:  python tests/mutate_deploy_core.py
Every mutant runs the entire real-Docker suite, so this takes hours rather than
minutes. That cost is the point: a mutant that is not run against every case
cannot be said to have been caught by the case named for it.
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
        """            if ($h.status -eq 'ok') { Good "${Phase}: /health status=ok" }
            else { $problems += "${Phase}: /health answered but status=$($h.status), not ok" }""",
        '''            Good "${Phase}: /health answered"''',
        ["CASE I"],
    ),
    (
        "build transport: ignore the build's exit code",
        """        if ($b.ExitCode -ne 0) {
            @($b.Output) | Select-Object -Last 15 | ForEach-Object { Say "    $_" }
            Stop-Deploy "the BUILD failed (exit $($b.ExitCode)). The old container is untouched, and $($cfg.ImageTag) still points at the last known-good image."
        }""",
        """        # mutant: build exit code ignored""",
        ["CASE A"],
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
for label, old, new, expect in MUTANTS:
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
if ok:
    print("VERDICT: every reviewed defect is caught by the case written for it")
else:
    print("VERDICT: %d MUTANT(S) SURVIVED -- those cases are not load bearing:" % len(survivors))
    for label, expect in survivors:
        print("   %-64s expected %s" % (label[:64], ", ".join(expect)))
print("  %s restored" % CORE)
sys.exit(0 if ok else 1)
