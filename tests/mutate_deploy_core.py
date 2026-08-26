"""Do the Docker fixture cases actually catch the defects they were written for?

Nine passing cases prove nothing on their own. A guard written beside the code
it checks passes BY CONSTRUCTION -- the only evidence that a case is load
bearing is that it FAILS when the defect it exists to catch is put back.

Each mutant below reintroduces one reviewed defect and names the case that must
fail. A mutant that SURVIVES is reported as a survivor, not quietly dropped and
not papered over by weakening the case.

Anchors are matched exactly once and the line numbers of the change are
printed, because a substring anchor that matches in two places silently mutates
the wrong site.

Run:  python tests/mutate_deploy_core.py
Takes roughly half an hour: every mutant runs the full real-Docker suite.
"""
import io
import re
import subprocess
import sys
import time

CORE = "scripts/deploy-core.ps1"
SUITE = "tests/test_deploy_core_docker.ps1"
ORIG = io.open(CORE, encoding="utf-8", newline="").read()

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
        """                $key = "$($cfg.ContainerPort)/tcp\"""",
        """                $key = "$($cfg.PortNum)/tcp\"""",
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
        """                if ($h.status -eq 'ok') { Good "/health status=ok" }
                else { $problems += "/health answered but status=$($h.status), not ok" }""",
        '''                Good "/health answered"''',
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
