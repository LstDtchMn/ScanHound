<#
  Real-Docker qualification for scripts/deploy-core.ps1.

  WHY THIS EXISTS. Two review rounds found design-level defects in the deploy
  script and both times the response was a rewrite whose invariants had been
  reasoned about and never executed. The reviewer's position was blunt and
  correct: the permission script had nineteen failure-injection cases and the
  deploy script -- the one that rebuilds production -- had none.

  WHAT IS DELIBERATELY NOT DONE HERE. Nothing replaces docker.exe with a fake.
  The properties under test are Docker state transitions, and a mock cannot
  qualify a Docker state transition; it can only qualify the mock. Every case
  below builds real images and creates real containers, in a disposable
  project with its own name, container, image tag and localhost port. The
  ScanHound container is never touched.

  Each case is stated as the failure it must produce, not the success it
  should allow. A deploy script that only ever passes its happy path is the
  thing the last two reviews rejected.

  Run:  powershell -ExecutionPolicy Bypass -File tests\test_deploy_core_docker.ps1
  Needs: docker, git, and the python:3.12-slim base image (local; no network).
#>

$ErrorActionPreference = 'Stop'
$RUNNER = Join-Path $PSScriptRoot 'deploy-fixture-runner.ps1'

function Native {
    <#
      Every native call in this file goes through here.

      PowerShell 5.1: redirecting a native program's stderr wraps each line in
      an ErrorRecord, and under $ErrorActionPreference='Stop' the first one
      TERMINATES -- so `docker inspect` on a missing container would abort the
      test rather than answer "absent". Exit codes are unaffected by any of
      that, which is why they are what gets returned.
    #>
    param([Parameter(Mandatory)][scriptblock]$Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $out  = & $Command 2>&1 | ForEach-Object { $_.ToString() }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
    [pscustomobject]@{ Output = @($out); ExitCode = $code; Text = (@($out) -join "`n") }
}

$PASS = 0; $FAIL = 0; $FAILED = @()
function Check([string]$CaseName, [scriptblock]$body) {
    # The parameter is NOT called $name. PowerShell variable names are
    # case-INSENSITIVE, so a parameter called $name shadowed the script-scope
    # $NAME (the fixture container) for the entire case body. Every deploy then
    # ran against a container named after the test case, and CASE A "passed"
    # only because both sides of its comparison were equally null.
    Write-Host ""
    Write-Host ("-- {0}" -f $CaseName) -ForegroundColor Cyan
    try {
        & $body
        $script:PASS++
        Write-Host ("  PASS  {0}" -f $CaseName) -ForegroundColor Green
    } catch {
        $script:FAIL++
        $script:FAILED += $CaseName
        Write-Host ("  FAIL  {0}`n          {1}" -f $CaseName, $_.Exception.Message) -ForegroundColor Red
    }
}
function Assert([bool]$cond, [string]$msg) { if (-not $cond) { throw $msg } }

# ---------------------------------------------------------------- fixture --

$SUFFIX = [guid]::NewGuid().ToString('N').Substring(0, 8)
$FX     = Join-Path $env:TEMP "shdeploy-$SUFFIX"
$ORIGIN = Join-Path $FX 'origin.git'
$WORK   = Join-Path $FX 'work'
$PINDIR = Join-Path $FX 'pinned'
$PINNED = Join-Path $PINDIR 'docker-compose.yml'
# Logs live OUTSIDE the fixture: the first run of this suite deleted every
# log in its own finally block, which made nine failures undebuggable.
$LOGDIR = Join-Path $env:TEMP "shdeploy-logs-$([datetime]::Now.ToString('yyyyMMdd-HHmmss'))"
$FXNAME   = "shfx$SUFFIX"
$TAG    = "${FXNAME}:latest"
$CAND   = "${FXNAME}:candidate-"

function Get-FreePort {
    $l = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $l.Start(); $p = $l.LocalEndpoint.Port; $l.Stop(); return $p
}
$PORT = Get-FreePort

function Git-Work {
    param([string[]]$A)
    $r = Native { git -C $WORK @A }
    if ($r.ExitCode -ne 0) { throw "git $($A -join ' ') failed (exit $($r.ExitCode)): $($r.Text)" }
}

$COMPOSE_V1 = @"
name: $FXNAME
services:
  app:
    build: .
    image: $TAG
    container_name: $FXNAME
    restart: "no"
    ports:
      - "127.0.0.1:${PORT}:8080"
"@

$DOCKERFILE_OK = @'
FROM python:3.12-slim
WORKDIR /app
COPY app/ /app/
EXPOSE 8080
CMD ["python", "/app/server.py"]
'@

$SERVER_PY = @'
import json, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

VERSION = open("/app/version.txt").read().strip()

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        # /degraded answers correctly but reports a non-ok status, so case I can
        # tell "the endpoint replied" apart from "the service is healthy".
        status = "degraded" if self.path.startswith("/degraded") else "ok"
        body = json.dumps({"status": status, "version": VERSION}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

def noise():
    while True:
        print("fixture heartbeat", flush=True)
        time.sleep(5)

threading.Thread(target=noise, daemon=True).start()
print("fixture %s starting" % VERSION, flush=True)
HTTPServer(("0.0.0.0", 8080), H).serve_forever()
'@

function New-FixtureRepo {
    New-Item -ItemType Directory -Force -Path $FX, $PINDIR, $LOGDIR | Out-Null
    Write-Host "   logs: $LOGDIR"
    $r = Native { git init --bare -b main $ORIGIN }
    if ($r.ExitCode -ne 0) { throw "git init --bare failed: $($r.Text)" }
    $r = Native { git clone $ORIGIN $WORK }
    if ($r.ExitCode -ne 0) { throw "git clone failed: $($r.Text)" }
    New-Item -ItemType Directory -Force -Path (Join-Path $WORK 'app') | Out-Null

    Set-Content -LiteralPath (Join-Path $WORK 'docker-compose.yml') -Value $COMPOSE_V1 -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $WORK 'Dockerfile')         -Value $DOCKERFILE_OK -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $WORK 'app\server.py')      -Value $SERVER_PY -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $WORK 'app\version.txt')    -Value 'V1' -Encoding ASCII
    # An ignore rule, so case D can prove that git-IGNORED content -- the kind
    # `git status --porcelain` never reports at all -- stays out of the image.
    Set-Content -LiteralPath (Join-Path $WORK '.gitignore')         -Value 'app/ignored_local.py' -Encoding ASCII

    Git-Work @('config','user.email','fixture@example.invalid')
    Git-Work @('config','user.name','fixture')
    Git-Work @('add','-A')
    Git-Work @('commit','-m','V1')
    Git-Work @('push','-u','origin','main')
    Copy-Item (Join-Path $WORK 'docker-compose.yml') $PINNED -Force
}

function Set-TargetVersion {
    <# Commit a new state and push it, so origin/main moves. #>
    param([string]$Version, [string]$Dockerfile = $DOCKERFILE_OK, [string]$Compose = $null)
    Set-Content -LiteralPath (Join-Path $WORK 'app\version.txt') -Value $Version -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $WORK 'Dockerfile') -Value $Dockerfile -Encoding ASCII
    if ($Compose) { Set-Content -LiteralPath (Join-Path $WORK 'docker-compose.yml') -Value $Compose -Encoding ASCII }
    Git-Work @('add','-A')
    Git-Work @('commit','-m',"target $Version")
    Git-Work @('push','origin','main')
    Git-Work @('fetch','origin','--prune')
}

$script:LASTLOG = $null
function Invoke-Deploy {
    <# Run the engine in a CHILD process, so a real process exit code is
       observed and not merely a verdict string. #>
    param([hashtable]$Extra = @{}, [string]$Hook = '', [string]$HookArg = '')
    $id      = [guid]::NewGuid().ToString('N').Substring(0,6)
    $cfgPath = Join-Path $FX "cfg-$id.json"
    $resPath = Join-Path $FX "res-$id.json"
    $logPath = Join-Path $LOGDIR "deploy-$id.log"
    $script:LASTLOG = $logPath
    $cfg = @{
        Repo             = $WORK
        PinnedCompose    = $PINNED
        Container        = $FXNAME
        Service          = 'app'
        ImageTag         = $TAG
        CandidatePrefix  = $CAND
        MutexName        = "Global\shdeploy-$SUFFIX"
        Ref              = 'origin/main'
        SkipPrGate       = $true
        HealthUrl        = "http://127.0.0.1:$PORT/"
        PortHost         = '127.0.0.1'
        PortNum          = $PORT
        ContainerPort    = 8080
        SettleSeconds    = 3
        LogWindowSeconds = 6
        SpamPattern      = 'fixture heartbeat'
        SpamThreshold    = 12
        WorkRoot         = $FX
        MutexTimeoutSec  = 30
    }
    foreach ($k in $Extra.Keys) { $cfg[$k] = $Extra[$k] }
    # Tripwire for the shadowing bug above: if any identity stops looking like
    # the fixture, fail loudly here rather than deploying against whatever the
    # name happened to resolve to.
    foreach ($k in @('Container','ImageTag','CandidatePrefix')) {
        if ($cfg[$k] -notlike "shfx$SUFFIX*") { throw "fixture identity '$k' is '$($cfg[$k])', not the fixture -- a variable is being shadowed" }
    }
    ($cfg | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $cfgPath -Encoding ASCII

    # -Hook is only passed when set: powershell -File DROPS an empty-string
    # argument, so '-Hook ""' reaches the runner as a bare '-Hook' with no
    # value and every case died with "Missing an argument for parameter 'Hook'".
    $argv = @('-NoProfile','-ExecutionPolicy','Bypass','-File',$RUNNER,
              '-ConfigPath',$cfgPath,'-ResultPath',$resPath)
    if ($Hook)    { $argv += @('-Hook', $Hook) }
    if ($HookArg) { $argv += @('-HookArg', $HookArg) }
    $r = Native { powershell @argv }
    Set-Content -LiteralPath $logPath -Value $r.Text -Encoding UTF8
    $res = $null
    if (Test-Path -LiteralPath $resPath) { $res = Get-Content -LiteralPath $resPath -Raw | ConvertFrom-Json }
    [pscustomobject]@{
        Exit    = $r.ExitCode
        Verdict = $(if ($res) { $res.Verdict } else { 'NO RESULT' })
        L       = $(if ($res) { $res.Ledger } else { $null })
        Log     = $logPath
    }
}

function Get-ImgId {
    param([string]$T)
    $r = Native { docker image inspect $T --format '{{.Id}}' }
    if ($r.ExitCode -ne 0 -or @($r.Output).Count -eq 0) { return $null }
    return $r.Output[0].Trim()
}
function Get-CtrId {
    $r = Native { docker inspect -f '{{.Id}}' $FXNAME }
    if ($r.ExitCode -ne 0 -or @($r.Output).Count -eq 0) { return $null }
    return $r.Output[0].Trim().Substring(0, 12)
}
function Get-CtrImg {
    $r = Native { docker inspect -f '{{.Image}}' $FXNAME }
    if ($r.ExitCode -ne 0 -or @($r.Output).Count -eq 0) { return $null }
    return $r.Output[0].Trim()
}
function Get-Version {
    for ($i = 0; $i -lt 10; $i++) {
        try { return (Invoke-RestMethod -Uri "http://127.0.0.1:$PORT/" -TimeoutSec 5).version } catch { Start-Sleep -Milliseconds 700 }
    }
    return $null
}

function Remove-Fixture {
    Native { docker rm -f $FXNAME } | Out-Null
    if (Test-Path -LiteralPath $PINNED) {
        Native { docker compose -f $PINNED --project-directory $WORK down --remove-orphans } | Out-Null
    }
    $imgs = (Native { docker image ls --format '{{.Repository}}:{{.Tag}}' }).Output | Where-Object { $_ -like "${FXNAME}:*" }
    foreach ($i in @($imgs)) { Native { docker image rm -f $i } | Out-Null }
    if (Test-Path -LiteralPath $WORK) { Native { git -C $WORK worktree prune } | Out-Null }
    Remove-Item -LiteralPath $FX -Recurse -Force -ErrorAction SilentlyContinue
}

# ===========================================================================
Write-Host ""
Write-Host "== deploy-core.ps1 -- real Docker qualification" -ForegroundColor Cyan
Write-Host "   fixture $FXNAME on 127.0.0.1:$PORT under $FX"

try {
    New-FixtureRepo
    Write-Host "   fixture repo created, V1 pushed"

    # -----------------------------------------------------------------------
    Check "SEED: a clean first deploy reaches VERIFIED and exits 0" {
        $r = Invoke-Deploy
        Assert ($r.Exit -eq 0) "expected exit 0, got $($r.Exit); verdict $($r.Verdict); log $($r.Log)"
        Assert ($r.Verdict -eq 'VERIFIED') "expected VERIFIED, got $($r.Verdict)"
        Assert ((Get-Version) -eq 'V1') "the running service does not report V1"
        Assert ((Get-ImgId $TAG) -eq (Get-CtrImg)) "$TAG does not point at the running image"
        Assert ($r.L.promoted -eq $true) "the ledger does not record promotion"
    }

    # -----------------------------------------------------------------------
    Check "OPS-2: the recovery tag still held the PREVIOUS image when the build ended" {
        $v1img = Get-ImgId $TAG
        Set-TargetVersion -Version 'V2'
        $r = Invoke-Deploy
        Assert ($r.Exit -eq 0) "expected exit 0, got $($r.Exit); log $($r.Log)"
        Assert ($r.L.recovery_tag_before -eq $v1img) "the ledger's pre-build tag is not the V1 image"
        Assert ($r.L.candidate_tag -like "$CAND*") "no candidate tag was used"
        Assert ((Get-Version) -eq 'V2') "the running service does not report V2"
        Assert ((Get-ImgId $TAG) -ne $v1img) "$TAG was never promoted to V2"
    }

    # -----------------------------------------------------------------------
    Check "CASE A: build failure leaves the old container running and untouched" {
        $before    = Get-CtrId
        $beforeTag = Get-ImgId $TAG
        Set-TargetVersion -Version 'V3' -Dockerfile @'
FROM python:3.12-slim
WORKDIR /app
COPY app/ /app/
RUN exit 7
EXPOSE 8080
CMD ["python", "/app/server.py"]
'@
        $r = Invoke-Deploy
        Assert ($r.Exit -ne 0) "a build failure must exit nonzero; got $($r.Exit)"
        Assert ($r.Verdict -ne 'VERIFIED') "a build failure must not verify"
        Assert ($r.L.build_exit -ne 0) "the ledger does not record a failed build"
        # Pins the MECHANISM, not just the outcome. Without this line the case
        # asserts only "the container was not replaced" -- which stays true when
        # the build exit code is ignored entirely, because the
        # candidate-does-not-exist guard stops the deploy anyway. A mutation run
        # proved exactly that: removing the exit-code check killed nothing.
        Assert ($r.L.stop_reason -like '*BUILD failed*') "the refusal is not the build-exit check: $($r.L.stop_reason)"
        Assert ((Get-CtrId) -eq $before) "the old container was replaced: $before -> $(Get-CtrId)"
        Assert ((Get-Version) -eq 'V2') "the old container is no longer serving V2"
        Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved despite a failed build"
        Assert ($r.L.promoted -ne $true) "the ledger claims promotion after a failed build"
    }

    # -----------------------------------------------------------------------
    Check "CASE B: build succeeds, activation fails, recovery tag still points at the last good image" {
        $beforeTag = Get-ImgId $TAG
        # Builds cleanly; the container cannot start, because its command does
        # not exist in the image. The compose recipe is UNCHANGED, so this is
        # an activation failure and not the SR2-1 drift refusal.
        Set-TargetVersion -Version 'V4' -Dockerfile @'
FROM python:3.12-slim
WORKDIR /app
COPY app/ /app/
EXPOSE 8080
CMD ["/nonexistent-binary"]
'@
        $r = Invoke-Deploy
        Assert ($r.Exit -ne 0) "an activation failure must exit nonzero; got $($r.Exit)"
        Assert ($r.Verdict -ne 'VERIFIED') "an activation failure must not verify"
        Assert ($r.L.build_exit -eq 0) "the build was supposed to SUCCEED here; exit $($r.L.build_exit); log $($r.Log)"
        Assert ($r.L.promoted -ne $true) "an unverified candidate was promoted"
        Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved to the unverified candidate -- the OPS-2 defect"
        Assert ($r.L.candidate_tag -and (Get-ImgId $r.L.candidate_tag)) "the candidate image does not exist"
        Assert ((Get-ImgId $r.L.candidate_tag) -ne $beforeTag) "the candidate is not a distinct image"

        # And the part that makes OPS-2 a production hazard rather than an
        # aesthetic one: run the SAME command the scheduled recovery task runs
        # and prove it cannot activate the failed candidate.
        Native { docker compose -f $PINNED --project-directory $WORK up -d --force-recreate --no-build --pull never } | Out-Null
        Assert ((Get-Version) -eq 'V2') "recovery did not restore the last verified service"
        Assert ((Get-CtrImg) -eq $beforeTag) "recovery activated the unverified candidate"
    }

    # -----------------------------------------------------------------------
    Check "CASE F: after a partial activation the observer reports actual state, not nulls" {
        # Same induced failure as case B; the assertion is about OPS-5.
        $r = Invoke-Deploy
        Assert ($r.Exit -ne 0) "expected a failure to observe"
        Assert ($null -ne $r.L.observed) "no observation block was produced after destructive work"
        $o = $r.L.observed
        foreach ($f in @('container_id','image_id','running','port','health','recovery_tag')) {
            Assert ($null -ne $o.$f -and "$($o.$f)" -ne '') "the observer left '$f' empty"
        }
        # The point of the finding: these are MEASURED after the error, not
        # replayed from fields populated before it. new_container_id is null
        # here because section 6 never ran, yet the observer still answers
        # "what is running right now".
        Assert ($null -eq $r.L.new_container_id) "section 6 was supposed to be unreachable in this case"
        Assert ($o.recovery_tag -ne 'UNKNOWN') "the observer could not read the recovery tag"
        Write-Host "        observed: container=$($o.container_id) running=$($o.running) health=$($o.health) tag=$($o.recovery_tag)"
    }

    # -----------------------------------------------------------------------
    Check "CASE E: compose drift against the TARGET recipe refuses BEFORE the build" {
        # Round 2 compared the pinned file against whatever was checked out,
        # in section 1, before merging. A target whose compose differs
        # therefore deployed cleanly and left recovery stale.
        $beforeCtr = Get-CtrId
        $drifted = $COMPOSE_V1 + "`n    environment:`n      - DRIFTED=yes`n"
        Set-TargetVersion -Version 'V5' -Compose $drifted
        $r = Invoke-Deploy
        Assert ($r.Exit -ne 0) "compose drift must exit nonzero; got $($r.Exit)"
        Assert ($r.L.build_attempted -ne $true) "it built before noticing the drift"
        Assert ($r.L.stop_reason -like '*pinned*') "the refusal does not name the pinned recipe: $($r.L.stop_reason)"
        try {
            Assert ((Get-CtrId) -eq $beforeCtr) "the container changed despite a refusal"
        } finally {
            # The fixture's compose MUST be restored even when an assertion above
            # throws -- the first run left it drifted and every later case
            # inherited the SR2-1 refusal instead of testing its own property.
            Set-TargetVersion -Version 'V5' -Compose $COMPOSE_V1
        }
    }

    # -----------------------------------------------------------------------
    Check "CASE D: untracked AND git-ignored local files stay out of the image" {
        # OPS-1 stated as a behaviour. The IGNORED file is the one that
        # matters: it never appears in `git status --porcelain` at all, so no
        # amount of tightening that filter could have caught it.
        Set-Content -LiteralPath (Join-Path $WORK 'app\untracked_local.py') -Value '# untracked' -Encoding ASCII
        Set-Content -LiteralPath (Join-Path $WORK 'app\ignored_local.py')   -Value '# ignored'   -Encoding ASCII
        $st = (Native { git -C $WORK status --porcelain }).Output
        Assert ([bool](@($st) -match 'untracked_local')) "the fixture did not create an untracked file"
        Assert (-not [bool](@($st) -match 'ignored_local')) "the ignored file should be invisible to git status"

        $r = Invoke-Deploy
        Assert ($r.Exit -eq 0) "expected a clean deploy, got exit $($r.Exit): $($r.L.stop_reason); log $($r.Log)"
        $ls = (Native { docker run --rm $r.L.candidate_tag ls /app }).Text
        Assert ($ls -notmatch 'untracked_local') "an UNTRACKED local file reached the image"
        Assert ($ls -notmatch 'ignored_local')   "a git-IGNORED local file reached the image"
        Assert ($ls -match 'server.py') "the image is missing the tracked source -- the two assertions above would prove nothing"
        Remove-Item (Join-Path $WORK 'app\untracked_local.py'), (Join-Path $WORK 'app\ignored_local.py') -Force
    }

    # -----------------------------------------------------------------------
    Check "CASE G: the deploy refuses when the recovery task holds the mutex" {
        # The mutex is this round's answer to OPS-2's in-flight race, and an
        # untested lock is not a lock. Contention is created for real: this
        # process takes the same named mutex the engine will ask for.
        $beforeCtr = Get-CtrId
        $beforeTag = Get-ImgId $TAG
        $m = New-Object System.Threading.Mutex($false, "Global\shdeploy-$SUFFIX")
        $held = $false
        try {
            $held = $m.WaitOne(0)
            Assert $held "the test could not take the fixture mutex -- the case would prove nothing"
            $r = Invoke-Deploy -Extra @{ MutexTimeoutSec = 5 }
            Assert ($r.Exit -ne 0) "a held mutex must exit nonzero; got $($r.Exit)"
            Assert ($r.L.stop_reason -like '*acquire*') "the refusal does not name the mutex: $($r.L.stop_reason)"
            # The lock is taken AFTER the build, deliberately: building writes
            # only the candidate tag and touches no container, so there is no
            # reason to block recovery for ten minutes. Asserted so that design
            # decision is visible rather than incidental.
            Assert ($r.L.build_attempted -eq $true) "the build was supposed to run before the lock is taken"
            Assert ((Get-CtrId) -eq $beforeCtr) "the container changed while the mutex was held elsewhere"
            Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved while the mutex was held elsewhere"
        } finally {
            if ($held) { $m.ReleaseMutex() }
            $m.Dispose()
        }
    }

    # -----------------------------------------------------------------------
    Check "CASE H: a host port that is not published is reported as a PROBLEM" {
        # The conflation this fixture already caught once: .NetworkSettings.Ports
        # is keyed by CONTAINER port, and the engine was building its key from
        # the HOST port. ScanHound maps 9721->9721, so production would have read
        # correct forever while asserting nothing.
        #
        # The old substring hazard ('127.0.0.1:97210' contains '127.0.0.1:9721')
        # is now structurally impossible -- HostPort is compared as an integer --
        # but the exactness is pinned here anyway.
        $r = Invoke-Deploy -Extra @{ PortNum = ($PORT + 1) }
        Assert ($r.Exit -ne 0) "an unpublished port must exit nonzero; got $($r.Exit)"
        Assert ($r.Verdict -eq 'PROBLEMS') "expected PROBLEMS, got $($r.Verdict)"
        Assert ([bool](@($r.L.problems) -match 'NOT bound')) "the port problem was not reported: $(@($r.L.problems) -join '; ')"
        Assert ($r.L.promoted -ne $true) "promoted despite an unbound port"
    }

    # -----------------------------------------------------------------------
    Check "CASE I: /health answering with a non-ok status is a PROBLEM, not a pass" {
        $r = Invoke-Deploy -Extra @{ HealthUrl = "http://127.0.0.1:$PORT/degraded" }
        Assert ($r.Exit -ne 0) "a degraded health status must exit nonzero; got $($r.Exit)"
        Assert ([bool](@($r.L.problems) -match 'status=degraded')) "the health status was not asserted: $(@($r.L.problems) -join '; ')"
        Assert ($r.L.promoted -ne $true) "promoted despite a degraded health status"
    }

    # -----------------------------------------------------------------------
    Check "CASE C: a container running an image other than the one built is refused" {
        $beforeTag = Get-ImgId $TAG
        Assert ([bool]$beforeTag) "no previous image to swap to -- this case would prove nothing"
        Set-TargetVersion -Version 'V6'
        $r = Invoke-Deploy -Hook 'SwapToOldImage' -HookArg $beforeTag
        Assert ($r.Exit -ne 0) "a wrong running image must exit nonzero; got $($r.Exit)"
        Assert ($r.L.stop_reason -like '*running something else*') "the refusal is not the identity check: $($r.L.stop_reason)"
        Assert ($r.L.promoted -ne $true) "promoted despite a wrong running image"
        try {
            Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved despite a wrong running image"
        } finally {
            # The hook replaced the container with a foreign `docker run` one
            # under the same container_name. Compose cannot adopt that, so
            # leaving it behind makes every later case fail with a name
            # conflict rather than testing its own property.
            Native { docker rm -f $FXNAME } | Out-Null
        }
    }

    # -----------------------------------------------------------------------
    Check "the fixture can be returned to a verified state afterwards" {
        Native { docker rm -f $FXNAME } | Out-Null
        $r = Invoke-Deploy
        Assert ($r.Exit -eq 0) "the fixture could not be returned to VERIFIED: $($r.L.stop_reason); log $($r.Log)"
        Assert ((Get-Version) -eq 'V6') "the restored service does not report V6"
    }
}
finally {
    Remove-Fixture
}

Write-Host ""
Write-Host ("== {0} passed, {1} failed" -f $PASS, $FAIL) -ForegroundColor $(if ($FAIL) { 'Red' } else { 'Green' })
foreach ($f in $FAILED) { Write-Host "   FAILED: $f" -ForegroundColor Red }
exit $(if ($FAIL) { 1 } else { 0 })
