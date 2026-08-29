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

# ===========================================================================
# THE INVARIANT TABLE
# ===========================================================================
# The reviewer asked for this instead of a case count, and the reason is exact:
# "18/18 passed" means every MODELLED state passed. It says nothing about the
# production invariants that were never modelled, and those are where the next
# outage lives. So the rows worth reading below are the NOT MODELLED ones.
#
# Printed at the end of every run, so it cannot quietly rot in a comment while
# the cases move.
$INVARIANTS = @'

== Invariant -> evidence

  SOURCE IDENTITY
    the built tree is exactly the target commit, with no untracked
      or git-ignored local content                          CASE D
    the clean worktree is really at the target SHA          NOT MODELLED - no case makes
                                                              `git worktree add` land elsewhere
    a freshly created worktree that is not clean is refused NOT MODELLED
    a merged PR's merge commit is an ancestor of the ref    NOT MODELLED - fixture runs
                                                              SkipPrGate; there is no GitHub
    every required PR check must be explicitly passing;
      pending/cancelled/absent are refusals                 NOT MODELLED - same reason.
                                                              tests/mutate_claude_permissions.py
                                                              covers no analogous path either

  BUILD AND QUARANTINE
    a failed build never deploys, even when a stale
      candidate for the same SHA already exists             CASE A2 (outcome)
                                                              CASE A  (refusal reason)
    the build does not move the recovery tag                CASE B
    the OPS-2 assertion itself firing                       NOT MODELLED - no case makes the
                                                              build write the recovery tag
    the compose build section is one this engine
      actually reproduces: the root as context, the
      default Dockerfile, and no build key this
      engine's plain docker build would drop                SR3-4 -- context, dockerfile
                                                              and args, each refused
                                                              before the build
    a SERVICE-level key that changes what a build
      produces -- platform:                                 SR3-4 platform
    target, secrets, ssh and dockerfile_inline by name      NOT MODELLED - all four share the
                                                              one allow-list branch that
                                                              `args` exercises; no case
                                                              names them
    a build that reports success but produced no image      NOT MODELLED

  RECIPE AGREEMENT
    pinned recovery recipe matches the target recipe,
      checked after target resolution, before the build     CASE E
    the SECOND drift check, immediately before activation   NOT MODELLED - no case edits the
                                                              pinned file between the two

  SERIALIZATION
    the engine honours the recovery lock NAME it is given   CASE G
    the PRODUCTION lock name is the one the recovery task
      actually takes                                        SR3-5 lock-name anchor
    the build stays OUTSIDE the recovery lock               CASE G (build_attempted)
    only one deploy runs at a time                          SR3-5 (a second run refuses)
                                                              plus SR3-5 still HELD (a run
                                                              in progress still owns the
                                                              lock, so an early RELEASE is
                                                              caught and not only an early
                                                              acquisition)
    the deploy-instance refusal cannot be confused with
      the recovery refusal                                  CASE G + SR3-5 (both directions)
    both locks are released on every exit path              NOT MODELLED - the suite would
                                                              deadlock rather than report

  ARTIFACT IDENTITY
    the container was actually replaced                     NOT MODELLED - no case makes
                                                              compose converge to the same one
    the running container is on the image just built        CASE C

  RUNTIME
    the container is running                                SR3-2 (post-reconcile)
    the exact host:port publish exists, keyed by the
      CONTAINER port                                        CASE H, and SEED via the
                                                              OPS-4 host-port mutant
    the required env var is set                             NOT MODELLED - the fixture sets no
                                                              RequireEnvVar; production asserts
                                                              SCANHOUND_DV_INGEST_KEY_SHA256
    /health answers with status=ok                          CASE I
    the log window: a flood is a PROBLEM                    NOT MODELLED as a failure. The
                                                              window is observed in every case
                                                              and never exceeded; it was never a
                                                              proof of the mechanism
    logs that cannot be read are UNKNOWN, not zero          NOT MODELLED

  STORAGE IDENTITY (SR3-1)
    host sources are the intended shares, proven BEFORE
      anything is activated against them                    SR3-1 host source
    a host proof that could not RUN is a refusal            SR3-1 cannot be obtained
    the container's binds resolve to those shares           SR3-1 container bind
    the critical destination is WRITABLE                    SR3-1 critical destination
    the critical destination is DELETABLE                   NOT MODELLED -- the case binds
      the volume :ro, so the probe fails at the write and
      the delete half is never separately reached
    a container proof that could not RUN is UNKNOWN         SR3-2 post-reconcile
    the spec is DERIVED from the target commit's
      mount-nas-shares.ps1                                  PARTIAL - the fixture lifts the
                                                              real RULE from the real file but
                                                              supplies its own mount LIST; the
                                                              nine-share derivation is covered
                                                              by tests/test_nas_probe_pin.ps1
    9p/UNC identity specifically                            NOT MODELLED - substituted by ext4
                                                              named volumes; see the SR3-1 note
                                                              above New-NasMounts
    NasProbe=false skips every storage proof                exercised by every non-NAS case,
                                                              asserted by none

  THE FINAL CONTAINER (SR3-2)
    the reconcile leaves the container on the pinned
      recipe and on the verified image                      SR3-2 reconcile
    the cheap checks run again against whatever the
      reconcile leaves running                              SR3-2 reconcile + post-reconcile
    a failed reconcile is a PROBLEM                         NOT MODELLED
    promotion happens only after full qualification         CASE B, CASE H, CASE I
    promotion is verified to have taken                     NOT MODELLED

  THE PROMOTION TRANSACTION (R4-101-1)
    a final-qualification failure REVERTS the promotion,
      and the recovery recipe then restores the PRIOR
      image -- proven by executing that recipe             R4-101-1 final-qualification
    the same, with the storage proofs enabled              SR3-2 post-reconcile
    the ledger records REVERTED distinctly from
      never-promoted                                       R4-101-1 final-qualification
                                                             + SR3-2 post-reconcile
    a first-ever deploy says plainly that no prior image
      exists to restore, instead of claiming a rollback    R4-101-1 FIRST-EVER
    a REVERTED promotion still gets the rollback offer;
      a FAILED revert does not                             SR3-6
    a failed RECONCILE reverts the promotion               NOT MODELLED -- no case makes
                                                             `compose up` return nonzero
    a Stop-Deploy AFTER promotion reverts it (the
      post-reconcile image mismatch, a failed inspect)     NOT MODELLED -- reaching it needs
                                                             the image to change between the
                                                             reconcile and the inspect
    a revert that does not take is reported as a REVERT
      FAILURE, not as a revert                             NOT MODELLED -- no case makes
                                                             `docker tag` fail
    a run KILLED between the tag move and the revert
      leaves a journal naming the image to restore, and
      the next run reports it                              R4-101-2 promotion journal
                                                             PARTIAL -- the interrupted run is
                                                             PLANTED. Nothing here kills the
                                                             engine at that instruction, so
                                                             what is proven is that the journal
                                                             is OPEN at that point and that one
                                                             found on disk is reported
    the journal survives a host reboot, not only a
      killed process                                       NOT MODELLED -- it is an ordinary
                                                             file next to the pinned recipe and
                                                             no case reboots anything
    a journal that cannot be WRITTEN warns and does not
      fail the deploy                                      NOT MODELLED -- no case makes the
                                                             pinned-recipe directory unwritable
    every promotion_state the engine writes reaches the
      wrapper branch written for it                        R4-101-2 promotion_state anchor
    no file claims the reverted tag names a VERIFIED
      image; it names the PRIOR one                        R4-101-2 PRIOR-image anchor
    an in-container probe that could not enter a container
      this run measured as DEAD is a CONSEQUENCE, not a
      storage failure                                      R4-101-2 storage decision rows
                                                             + R4-101-2 wrapper summary
    the revert happens while the RECOVERY MUTEX is still
      held                                                 PARTIAL -- R4-101-1
                                                             final-qualification asserts the
                                                             OBSERVER (which runs before the
                                                             release) already saw the prior
                                                             image. There is no seam between
                                                             the revert and the release to
                                                             probe from a second process, the
                                                             way SR3-5 probes the deploy lock
    the recreate is NOT recommended after a storage
      failure                                             R4-101-1 storage failure (decision)
                                                             + R4-101-2 wrapper summary, which
                                                             LIFTS and EXECUTES the wrapper's
                                                             own NOT-VERIFIED block between its
                                                             markers, in both directions -- so
                                                             the PRINTING is modelled now

  DISCLOSURE AND DRY RUN
    after destructive work, what is running NOW is
      measured, not replayed                                CASE F
    the observer can never throw                            NOT MODELLED - no case removes
                                                              docker from the child process
    the rollback offer is driven by the observer            SR3-6
    -WhatIf makes no merge, build, tag, recreation or
      production mutation, and cleans up its worktree       SR3-7 (build/tag/recreate only)
    the plan-only line does not claim "nothing was
      changed", and the playbook quotes what the wrapper
      actually prints                                      SR3-7 plan-only wording
    -WhatIf makes no PR MERGE                             NOT MODELLED -- the fixture runs
      SkipPrGate, so the WhatIf merge guard is never
      reached by any case or mutant
    -WhatIf with -Prs has NOT qualified the post-merge
      tree                                                  NOT MODELLED - documented contract
                                                              only; it cannot be modelled without
                                                              a GitHub the fixture does not have

  CLEANUP
    the worktree is removed and pruned                      SR3-7
    the candidate override file is removed                  NOT MODELLED
'@

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
# SR3-5. TWO locks, because the engine now takes two and the whole point of
# the finding is that they are not interchangeable. The RECOVERY lock
# (Global\shdeploy-<suffix>, contended by CASE G) is shared with the mount
# recovery task and is held only around the container transition. The
# DEPLOY-INSTANCE lock below is held for the entire run and serialises deploy
# against deploy.
$RECMUTEX    = "Global\shdeploy-$SUFFIX"
$DEPLOYMUTEX = "Global\shdeployrun-$SUFFIX"

# ---------------------------------------------------------------- SR3-1 ----
# Storage identity, and what this fixture can and cannot model.
#
# CANNOT: create a 9p mount of a UNC share. There is no NAS here and no WSL
# distro to mount one in.
#
# CAN, and does: create the exact SHAPE of the production failure. The lifted
# probe asks three questions of every target -- is it a mountpoint, is its
# filesystem type the expected one, does its mountinfo line carry the expected
# ORIGIN -- and then asks whether the critical destination can be written to
# and cleaned up. Docker named volumes answer all four questions with real
# kernel state: measured on this host, /library/tv backed by a named volume
# reports
#
#   ... /data/docker/volumes/<name>/_data /library/tv rw,relatime ... - ext4 ...
#
# so a DIFFERENT volume bound at the same target is a genuine origin mismatch,
# and the same volume bound :ro is a genuinely unwritable destination. The
# cases below are the same defects production would suffer, with ext4 and a
# volume path standing in for 9p and a UNC path. That substitution is exactly
# the reason NasFsType and the per-mount Origin are configuration and not
# constants in the engine.
$VOLTV    = "${FXNAME}tv"
$VOLSRC   = "${FXNAME}src"
$VOLDECOY = "${FXNAME}decoy"
$NASFS    = 'ext4'
$TVTARGET = '/library/tv'
$SRCTARGET = '/library/plex-source/one'

# The real recovery script. The fixture copies it into its own repo so the
# engine lifts the identity rule from the SAME file production uses -- if that
# rule changes shape, these cases fail here rather than in production.
$MOUNTSCRIPT = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\mount-nas-shares.ps1'

function New-NasMounts {
    <# The correct spec. $TvSource overrides only where /library/tv is expected
       to come from, so a case can make the HOST source wrong while leaving the
       container's own binds correct. #>
    param([string]$TvSource = $VOLTV)
    return @(
        @{ HostPath = $TvSource; Target = $TVTARGET;  Origin = "/volumes/$VOLTV/_data";  ReadOnly = $false },
        @{ HostPath = $VOLSRC;   Target = $SRCTARGET; Origin = "/volumes/$VOLSRC/_data"; ReadOnly = $true  }
    )
}
function New-NasConfig {
    param([string]$TvSource = $VOLTV)
    return @{
        NasProbe           = $true
        NasMounts          = (New-NasMounts -TvSource $TvSource)
        NasCriticalTarget  = $TVTARGET
        NasFsType          = $NASFS
        NasProbeTimeoutSec = 60
    }
}

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

# Three recipes that differ ONLY in how /library/tv is bound. Everything the
# other checks look at -- image, port, env, health -- is identical in all
# three, which is the point: those checks pass in every one of them.
function New-NasCompose {
    param([string]$TvVolume = $VOLTV, [string]$TvMode = '')
    $suffix = $(if ($TvMode) { ":$TvMode" } else { '' })
    return @"
name: $FXNAME
services:
  app:
    build: .
    image: $TAG
    container_name: $FXNAME
    restart: "no"
    ports:
      - "127.0.0.1:${PORT}:8080"
    volumes:
      - "${TvVolume}:${TVTARGET}$suffix"
      - "${VOLSRC}:${SRCTARGET}:ro"
volumes:
  ${VOLTV}:
    external: true
  ${VOLSRC}:
    external: true
  ${VOLDECOY}:
    external: true
"@
}

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
    New-Item -ItemType Directory -Force -Path (Join-Path $WORK 'scripts') | Out-Null

    # SR3-1. The REAL recovery script, copied in unmodified. The engine lifts
    # the identity rule out of the target commit's copy of it, so this is what
    # makes these cases exercise production's actual rule rather than a
    # restatement of it. Copying is read-only with respect to the live
    # Scheduled Task's own file.
    if (-not (Test-Path -LiteralPath $MOUNTSCRIPT)) { throw "the recovery script is missing at $MOUNTSCRIPT" }
    Copy-Item -LiteralPath $MOUNTSCRIPT -Destination (Join-Path $WORK 'scripts\mount-nas-shares.ps1') -Force

    foreach ($v in @($VOLTV, $VOLSRC, $VOLDECOY)) {
        $r = Native { docker volume create $v }
        if ($r.ExitCode -ne 0) { throw "could not create the fixture volume ${v}: $($r.Text)" }
    }
    # The read-only source must not be empty for the identity check to be
    # meaningful about a real directory, and the decoy must be distinguishable.
    Native { docker run --rm -v "${VOLSRC}:/v" -v "${VOLDECOY}:/w" python:3.12-slim sh -c 'touch /v/seed /w/seed' } | Out-Null

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
        MutexName        = $RECMUTEX
        DeployMutexName  = $DEPLOYMUTEX
        Ref              = 'origin/main'
        SkipPrGate       = $true
        HealthUrl        = "http://127.0.0.1:$PORT/"
        PortHost         = '127.0.0.1'
        PortNum          = $PORT
        ContainerPort    = 8080
        SettleSeconds    = 3
        # Low ceiling for the health POLL: the fixture app answers in ~2s when
        # alive, and the cases that expect a DEAD container must not wait a
        # production-sized startup window to prove it.
        HealthTimeoutSeconds = 8
        LogWindowSeconds = 6
        SpamPattern      = 'fixture heartbeat'
        SpamThreshold    = 12
        WorkRoot         = $FX
        MutexTimeoutSec  = 30
    }
    foreach ($k in $Extra.Keys) { $cfg[$k] = $Extra[$k] }
    # Tripwire for the shadowing bug above, and now for a SECOND one it was
    # too weak to catch. CASE A2 was first written with a local called $cand
    # while the fixture's candidate prefix is $CAND -- the SAME variable, since
    # PowerShell names are case-insensitive -- so from that case onward the
    # prefix was "shfx<suffix>:candidate-<sha12>". A prefix match still
    # accepted it. The engine then built a candidate carrying the sha TWICE,
    # could not find it afterwards, and CASE A2 passed on a guard that had
    # nothing to do with the build exit code it exists to pin. The mutation
    # run is what surfaced it: the build-exit mutant SURVIVED.
    #
    # So this is EXACT equality against literals, not a prefix match against a
    # variable. A tripwire built out of the values it is checking cannot see
    # them change together.
    $expect = @{
        Container       = "shfx$SUFFIX"
        ImageTag        = "shfx${SUFFIX}:latest"
        CandidatePrefix = "shfx${SUFFIX}:candidate-"
        MutexName       = "Global\shdeploy-$SUFFIX"
        DeployMutexName = "Global\shdeployrun-$SUFFIX"
    }
    foreach ($k in $expect.Keys) {
        if ($cfg[$k] -cne $expect[$k]) {
            throw "fixture identity '$k' is '$($cfg[$k])', expected '$($expect[$k])' -- a variable is being shadowed"
        }
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
    foreach ($v in @($VOLTV, $VOLSRC, $VOLDECOY)) { Native { docker volume rm -f $v } | Out-Null }
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
    Check "SR3-5: the PRODUCTION recovery lock name is the one the recovery task takes" {
        # Source-level, no Docker. CASE G proves the engine honours whatever
        # mutex name its CONFIG supplies -- it says nothing about whether the
        # production config supplies the RIGHT one. Both files hard-code the
        # name independently, so a rename in either leaves this whole suite
        # green while the deploy lock and the recovery lock silently stop being
        # the same lock. That is the hazard the shared mutex exists to remove,
        # so it is anchored here the way test_nas_probe_pin.ps1 anchors the NAS
        # rule rather than trusted to stay in step.
        $wrapper  = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\merge-and-deploy.ps1'
        $recovery = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\mount-nas-shares.ps1'
        foreach ($f in @($wrapper, $recovery)) {
            Assert (Test-Path -LiteralPath $f) "cannot find $f"
        }
        $wTxt = Get-Content -LiteralPath $wrapper  -Raw
        $rTxt = Get-Content -LiteralPath $recovery -Raw

        $wm = [regex]::Matches($wTxt, "(?m)^\s*MutexName\s*=\s*'([^']+)'")
        Assert ($wm.Count -eq 1) "expected exactly one MutexName assignment in merge-and-deploy.ps1, found $($wm.Count)"
        $deployName = $wm[0].Groups[1].Value

        $rm = [regex]::Matches($rTxt, 'New-Object System\.Threading\.Mutex\([^,]+,\s*"([^"]+)"\)')
        Assert ($rm.Count -eq 1) "expected exactly one named Mutex in mount-nas-shares.ps1, found $($rm.Count)"
        $recoveryName = $rm[0].Groups[1].Value

        Write-Host "        wrapper  MutexName : $deployName"
        Write-Host "        recovery Mutex     : $recoveryName"
        Assert ($deployName -eq $recoveryName) (
            "the deploy wrapper takes '$deployName' but the recovery task takes " +
            "'$recoveryName'. They are not the same lock, so recovery can recreate " +
            "the container in the middle of a deploy.")
    }

    # -----------------------------------------------------------------------
    Check "SR3-7: the wrapper's plan-only line and the playbook quote the same wording" {
        # SR3-7 reopened at round 4. The wrapper printed "PLAN ONLY  nothing was
        # changed." after a -WhatIf that fetches and prunes this repository's
        # remote-tracking refs and creates and removes a git worktree. Those are
        # real writes under .git. The engine's own -WhatIf banner had already
        # been corrected to say so; the operator-facing summary line and the
        # playbook that quotes it had not.
        #
        # Source-level and anchored in BOTH files, like the SR3-5 lock-name
        # case, because the failure mode is drift between two files that each
        # hard-code the sentence: fixing the script and leaving the playbook
        # quoting the old line puts the false claim in front of the operator
        # anyway.
        $wrapper  = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\merge-and-deploy.ps1'
        $playbook = Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\runbooks\2026-08-28-first-supervised-deploy-run.md'
        foreach ($f in @($wrapper, $playbook)) { Assert (Test-Path -LiteralPath $f) "cannot find $f" }
        $wTxt = Get-Content -LiteralPath $wrapper  -Raw
        $pTxt = Get-Content -LiteralPath $playbook -Raw

        $m = [regex]::Matches($wTxt, 'PLAN ONLY[^"]*')
        Assert ($m.Count -eq 1) "expected exactly one PLAN ONLY line in merge-and-deploy.ps1, found $($m.Count)"
        $line = $m[0].Value
        Write-Host "        wrapper prints : $line"
        Assert ($line -notmatch 'nothing was changed') `
            ("SR3-7: the wrapper still claims -WhatIf changed nothing. It fetches and prunes " +
             "git refs and creates and removes a worktree: '$line'")
        Assert ($line -eq 'PLAN ONLY - no production state changed.') "unexpected plan-only wording: '$line'"
        Assert ($pTxt -match [regex]::Escape($line)) `
            "the playbook does not quote the wording the wrapper actually prints: '$line'"
        Assert ($pTxt -notmatch 'nothing was changed') "the playbook still quotes the old, false wording"
    }

    # -----------------------------------------------------------------------
    Check "R4-101-2: every promotion_state the engine writes is classified by the wrapper" {
        # S3. A cross-FILE contract with no anchor. scripts/deploy-core.ps1
        # writes five exact promotion_state strings; scripts/merge-and-deploy.ps1
        # classifies them with -like '*REVERT FAILED*' / '*NO PRIOR IMAGE*' /
        # '*REVERTED*'. Neither file mentions the other, and a reword on either
        # side falls through SILENTLY into the wrong paragraph:
        #   a renamed REVERT-FAILED lands in the red "this should not be
        #     reachable" block, which tells the operator to report a bug
        #     instead of telling them to repoint the tag by hand;
        #   a renamed REVERTED prints the never-promoted paragraph, which says
        #     the candidate never entered the recovery namespace. It did.
        # R4-101-1 anchored the SR3-5 lock name and the SR3-7 plan-only line
        # and left these -- the strings the rollback advice is keyed on --
        # unanchored. So: read BOTH files and run the classification.
        $core    = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\deploy-core.ps1'
        $wrapper = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\merge-and-deploy.ps1'
        foreach ($f in @($core, $wrapper)) { Assert (Test-Path -LiteralPath $f) "cannot find $f" }
        $cTxt = Get-Content -LiteralPath $core    -Raw
        $wTxt = Get-Content -LiteralPath $wrapper -Raw

        # Every literal the engine can put in promotion_state, including the
        # initialiser. Read out of the file, so a NEW state added without a
        # wrapper branch fails here rather than shipping unclassified.
        $states = @([regex]::Matches($cTxt, "promotion_state\s*=\s*'([^']*)'") |
                    ForEach-Object { $_.Groups[1].Value } | Select-Object -Unique | Sort-Object)
        $expected = @(
            'never promoted',
            'promoted',
            'promoted, then REVERTED to the prior image',
            'promoted; NO PRIOR IMAGE existed to restore',
            'promoted; the REVERT FAILED') | Sort-Object
        Write-Host "        engine states  : $($states -join ' | ')"
        Assert (@($states).Count -eq @($expected).Count -and (@(Compare-Object $states $expected).Count -eq 0)) `
            ("the engine's promotion_state values have changed. found: $($states -join ' | ') " +
             "-- expected: $($expected -join ' | '). Every value the wrapper classifies is " +
             "hard-coded there too, so a reword here changes which paragraph the operator reads.")

        # The wrapper's classifier, in ORDER, because the chain is first-match.
        $pats = @([regex]::Matches($wTxt, "\`$pstate -like '([^']+)'") | ForEach-Object { $_.Groups[1].Value })
        Write-Host "        wrapper branch : $($pats -join ' -> ')"
        Assert (@($pats).Count -eq 3) "expected 3 promotion_state patterns in the wrapper, found $(@($pats).Count)"

        function Classify([string]$State) {
            for ($i = 0; $i -lt @($pats).Count; $i++) { if ($State -like $pats[$i]) { return $pats[$i] } }
            return 'FALL-THROUGH'
        }
        # 'never promoted' and 'promoted' are BOTH meant to fall through the
        # -like chain -- the wrapper separates them with the `promoted` flag,
        # not with a pattern -- so FALL-THROUGH is the correct answer for both
        # and a wrong answer for the other three.
        $want = @{
            'never promoted'                             = 'FALL-THROUGH'
            'promoted'                                   = 'FALL-THROUGH'
            'promoted, then REVERTED to the prior image' = '*REVERTED*'
            'promoted; NO PRIOR IMAGE existed to restore' = '*NO PRIOR IMAGE*'
            'promoted; the REVERT FAILED'                = '*REVERT FAILED*'
        }
        foreach ($s in $states) {
            Assert ($want.ContainsKey($s)) "the engine writes promotion_state '$s', which this case has no expectation for"
            $got = Classify $s
            Write-Host ("        {0,-44} -> {1}" -f $s, $got)
            Assert ($got -eq $want[$s]) `
                ("'$s' is classified by the wrapper as '$got', not '$($want[$s])'. " +
                 "The operator gets the wrong paragraph and the wrong next command.")
        }
    }

    # -----------------------------------------------------------------------
    Check "R4-101-2: nothing claims the reverted tag names a VERIFIED image" {
        # S2. Invoke-PromotionRevert said "The recovery recipe now restores the
        # last VERIFIED image again", the wrapper said "It points at the last
        # verified image again", and the playbook said "the tag names the last
        # verified image". What is actually restored is recovery_tag_before --
        # the tag VALUE when the build started. MEASURED on the live host:
        # scanhound:latest is sha256:78324087070f, created 2026-08-26 by a HAND
        # build that never went through this engine, and the same playbook
        # paragraph says the repo cannot prove what that is. A run that ends
        # 'promoted; the REVERT FAILED' also leaves an unqualified image there.
        #
        # Anchored across all three files for the SR3-7 reason: correcting the
        # script and leaving the playbook quoting the old claim puts it in front
        # of the operator anyway.
        $files = @{
            core     = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\deploy-core.ps1'
            wrapper  = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\merge-and-deploy.ps1'
            playbook = Join-Path (Split-Path -Parent $PSScriptRoot) 'docs\runbooks\2026-08-28-first-supervised-deploy-run.md'
        }
        foreach ($k in $files.Keys) { Assert (Test-Path -LiteralPath $files[$k]) "cannot find $($files[$k])" }
        foreach ($k in $files.Keys) {
            $txt = Get-Content -LiteralPath $files[$k] -Raw
            $bad = @([regex]::Matches($txt, '(?i)last\s+(verified|known-good)\s+image'))
            if (@($bad).Count) {
                foreach ($b in $bad) {
                    $at = $txt.Substring([Math]::Max(0, $b.Index - 70), [Math]::Min(150, $txt.Length - [Math]::Max(0, $b.Index - 70)))
                    Write-Host "        $k : ...$($at -replace '\s+', ' ')..."
                }
            }
            Assert (@($bad).Count -eq 0) `
                ("$k still claims the recovery tag names the 'last verified/known-good image'. " +
                 "It names the PRIOR image -- the tag value when the run started -- and nothing " +
                 "in this repo can prove that image was ever qualified by this engine.")
        }
        # Positive, so the case cannot be satisfied by deleting the sentence.
        $cTxt = Get-Content -LiteralPath $files.core -Raw
        Assert ($cTxt -match 'restores that PRIOR image again') `
            "the revert no longer tells the operator WHAT it restored"
        Assert ($cTxt -match 'ever qualified BY it') `
            "the revert message no longer states the limit of what it can prove"
        $pTxt = Get-Content -LiteralPath $files.playbook -Raw
        Assert ($pTxt -match 'Prior, not "last verified"') `
            "the playbook no longer explains that PRIOR is not the same claim as verified"
    }

    # -----------------------------------------------------------------------
    Check "R4-101-1: the recreate is not recommended after a storage failure" {
        # A pure decision, exercised directly. The wrapper's one-command
        # rollback CREATES a container, and Docker resolves bind SOURCES at
        # container-create time -- so recommending it after a storage proof
        # failed would tell the operator to bind /library/tv to whatever those
        # paths currently are. That is the 2026-07-26 outage, on purpose.
        #
        # The only passing shape is reason 'probed' with code 0. Everything
        # else -- including a phase that could not be measured -- is a storage
        # failure, because UNKNOWN is not proven.
        . (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\deploy-core.ps1')

        function S([hashtable]$Over) {
            $b = @{
                nas_host_reason = $null; nas_host_code = $null
                nas_candidate_reason = $null; nas_candidate_code = $null
                nas_final_reason = $null; nas_final_code = $null
            }
            foreach ($k in $Over.Keys) { $b[$k] = $Over[$k] }
            return $b
        }

        # A phase that never ran says nothing either way, and 'n/a' means the
        # storage proofs are switched off for this deployment.
        Assert (-not (Test-StorageFailureObserved -Ledger (S @{}))) `
            "a run with no storage phases at all reported a storage failure"
        Assert (-not (Test-StorageFailureObserved -Ledger (S @{
            nas_host_reason = 'n/a'; nas_host_code = 'n/a'
            nas_candidate_reason = 'n/a'; nas_candidate_code = 'n/a'
            nas_final_reason = 'n/a'; nas_final_code = 'n/a' }))) "'n/a' was read as a failure"
        Assert (-not (Test-StorageFailureObserved -Ledger (S @{
            nas_host_reason = 'probed'; nas_host_code = 0
            nas_candidate_reason = 'probed'; nas_candidate_code = 0
            nas_final_reason = 'probed'; nas_final_code = 0 }))) `
            "a fully proven run reported a storage failure"

        # Every phase, on its own, in both shapes a phase can fail: it ran and
        # said NO, and it could not be run at all.
        foreach ($p in @('host','candidate','final')) {
            Assert (Test-StorageFailureObserved -Ledger (S @{ "nas_${p}_reason" = 'probed'; "nas_${p}_code" = 2 })) `
                "a nonzero $p probe code was not read as a storage failure"
            Assert (Test-StorageFailureObserved -Ledger (S @{ "nas_${p}_reason" = 'not-running'; "nas_${p}_code" = $null })) `
                "an UNRUNNABLE $p probe was not read as a storage failure -- UNKNOWN is not proven"
            # The row that separates the two checks. A probe that did not run
            # can still carry code 0 -- Invoke-NasProbeInContainer reports a
            # reason and a code independently, and 0 is the default of an int
            # that was never assigned a real exit status. Without this row a
            # decision that looked ONLY at the code would keep passing the case
            # above on the strength of a null, and the reason check would not
            # be load bearing at all.
            Assert (Test-StorageFailureObserved -Ledger (S @{ "nas_${p}_reason" = 'host-container-failed'; "nas_${p}_code" = 0 })) `
                "a $p probe that did not RUN was read as a pass because its code happened to be 0"
        }

        # Codes reach the wrapper as numbers in production and as whatever
        # ConvertFrom-Json produces in the fixture. A decision that only works
        # on one of those works in the test and not in the field.
        Assert (Test-StorageFailureObserved -Ledger (S @{ nas_final_reason = 'probed'; nas_final_code = '2' })) `
            "a probe code that arrived as a STRING was read as a pass"
        Assert (-not (Test-StorageFailureObserved -Ledger (S @{ nas_final_reason = 'probed'; nas_final_code = '0' }))) `
            "the string '0' was read as a failure"
        $viaJson = (S @{ nas_final_reason = 'probed'; nas_final_code = 2 }) | ConvertTo-Json -Depth 5 | ConvertFrom-Json
        Assert (Test-StorageFailureObserved -Ledger $viaJson) `
            "the decision does not survive the JSON round trip the ledger takes"

        # -------------------------------------------------------------------
        # R4-101-2. The exemption, and the four boundaries that keep it narrow.
        #
        # THE FINDING. Promotion requires zero problems AND zero unknowns at the
        # candidate phase, and the host source proof is a Stop-Deploy gate -- so
        # on EVERY post-promotion failure with the probes enabled, host and
        # candidate read 'probed / 0' while the dead final container makes
        # nas_final_reason 'not-running'. That is precisely the ledger the SR3-2
        # post-reconcile case above asserts, and it was being classified as a
        # STORAGE failure: the operator was told a storage proof had failed, on
        # a run whose two SOURCE proofs had both passed and printed so.
        $deadFinal = S @{
            nas_host_reason = 'probed'; nas_host_code = 0
            nas_candidate_reason = 'probed'; nas_candidate_code = 0
            nas_final_reason = 'not-running'; nas_final_code = $null
            final_container_running = $false }
        Assert (-not (Test-StorageFailureObserved -Ledger $deadFinal)) `
            "a probe that could not enter a container this run already measured as DEAD was read as a storage failure"
        # ...and the same shape one phase earlier.
        Assert (-not (Test-StorageFailureObserved -Ledger (S @{
            nas_host_reason = 'probed'; nas_host_code = 0
            nas_candidate_reason = 'not-running'; nas_candidate_code = $null
            candidate_container_running = $false }))) `
            "a dead CANDIDATE container's probe was read as a storage failure"

        # BOUNDARY 1, and the one that keeps the exemption from swallowing the
        # thing it is supposed to detect: a probe that RAN and said the binds
        # are wrong is a storage failure whether or not the container later
        # died. Without this row the exemption could be widened to "the
        # container is dead, ignore its storage result" and nothing would
        # notice.
        Assert (Test-StorageFailureObserved -Ledger (S @{
            nas_host_reason = 'probed'; nas_host_code = 0
            nas_candidate_reason = 'probed'; nas_candidate_code = 0
            nas_final_reason = 'probed'; nas_final_code = 2
            final_container_running = $false })) `
            "a probe that RAN and FAILED stopped counting because the container was later dead"
        # BOUNDARY 2: a probe that could not run for some OTHER reason is still
        # UNKNOWN, and UNKNOWN is not proven -- dead container or not.
        Assert (Test-StorageFailureObserved -Ledger (S @{
            nas_final_reason = 'timeout'; nas_final_code = $null
            final_container_running = $false })) `
            "a TIMED-OUT probe was exempted; only 'not-running' is a consequence of a dead container"
        # BOUNDARY 3: the exemption needs this run's OWN liveness measurement.
        # A ledger that never recorded one -- an older one, a hand-built one, a
        # phase whose running state could not be READ -- gets no exemption.
        Assert (Test-StorageFailureObserved -Ledger (S @{ nas_final_reason = 'not-running'; nas_final_code = $null })) `
            "'not-running' was exempted with NO liveness measurement to justify it"
        Assert (Test-StorageFailureObserved -Ledger (S @{
            nas_final_reason = 'not-running'; nas_final_code = $null
            final_container_running = $true })) `
            "the probe said not-running while the run measured the container ALIVE; that is unexplained, not exempt"
        # BOUNDARY 4: the HOST proof is never exempt. It runs in a throwaway
        # container this engine creates, not in the service container, and it
        # is the proof that describes what Docker resolves at container-CREATE
        # time -- which is the only reason this decision exists.
        Assert (Test-StorageFailureObserved -Ledger (S @{
            nas_host_reason = 'not-running'; nas_host_code = $null
            candidate_container_running = $false; final_container_running = $false })) `
            "the HOST source proof was exempted by a dead SERVICE container"

        # And the shape it actually arrives in.
        $deadViaJson = $deadFinal | ConvertTo-Json -Depth 5 | ConvertFrom-Json
        Assert (-not (Test-StorageFailureObserved -Ledger $deadViaJson)) `
            "the exemption does not survive the JSON round trip the ledger takes"
    }

    # -----------------------------------------------------------------------
    Check "R4-101-2: after a dead final container the wrapper offers the ROLLBACK FIRST and prints no storage alarm" {
        # The CONSUMER, not a proxy for it. The decision case above pins
        # Test-StorageFailureObserved; this one runs the paragraphs the
        # operator actually reads, by lifting them out of
        # scripts/merge-and-deploy.ps1 between its two markers and EXECUTING
        # them. A case that re-implemented those branches could not have found
        # the defect, because the defect was in the branches.
        . (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\deploy-core.ps1')
        $wrapper = Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\merge-and-deploy.ps1'
        Assert (Test-Path -LiteralPath $wrapper) "cannot find $wrapper"
        $wTxt = Get-Content -LiteralPath $wrapper -Raw
        $mk = [regex]::Match($wTxt,
            '# >>> R4-101-2 SUMMARY BLOCK BEGIN(?<body>.*?)# >>> R4-101-2 SUMMARY BLOCK END',
            [System.Text.RegularExpressions.RegexOptions]::Singleline)
        Assert ($mk.Success) "the wrapper's NOT-VERIFIED summary markers are gone; this case cannot see what the operator reads"
        Assert (([regex]::Matches($wTxt, 'R4-101-2 SUMMARY BLOCK BEGIN')).Count -eq 1) "more than one BEGIN marker"
        $block = [scriptblock]::Create($mk.Groups['body'].Value)

        # The identities the lifted block prints. Deliberately not the fixture's,
        # so a path that leaked in from elsewhere would be obvious.
        $cfg = @{
            ImageTag      = 'lifted:latest'
            Repo          = 'X:\lifted\repo'
            PinnedCompose = 'C:\lifted\docker-compose.yml'
        }
        function Run-Summary($Ledger) {
            $result = [pscustomobject]@{ Verdict = 'PROBLEMS'; Ledger = $Ledger }
            return @(& $block 6>&1 | ForEach-Object { "$_" })
        }
        function IndexOf($Lines, [string]$Needle) {
            for ($i = 0; $i -lt @($Lines).Count; $i++) { if ($Lines[$i] -like "*$Needle*") { return $i } }
            return -1
        }

        # THE EXACT LEDGER a post-promotion failure produces with the probes on
        # and the final container dead: both SOURCE proofs passed, the promotion
        # was reverted, and the container really was replaced.
        function DeadFinalLedger([hashtable]$Over) {
            $b = @{
                promoted          = $false
                promotion_state   = 'promoted, then REVERTED to the prior image'
                old_container_id  = 'aaaaaaaaaaaa'
                new_container_id  = 'bbbbbbbbbbbb'
                merged_prs        = @()
                recovery_tag_before = 'sha256:1111111111'
                nas_host_reason      = 'probed';      nas_host_code = 0
                nas_candidate_reason = 'probed';      nas_candidate_code = 0
                nas_final_reason     = 'not-running'; nas_final_code = $null
                candidate_container_running = $true
                final_container_running     = $false
                promotion_journal = 'closed'
                interrupted_prior_promotion = $null
                observed = @{ container_id = 'bbbbbbbbbbbb'; running = 'false'; health = 'none' }
            }
            foreach ($k in $Over.Keys) { $b[$k] = $Over[$k] }
            return $b
        }

        # The premises, so the conclusion cannot pass for the wrong reason: the
        # rollback must be ADVISABLE at all, or "offered first" would be vacuous.
        $dead = DeadFinalLedger @{}
        Assert (Test-RollbackAdvisable -Ledger $dead) "the rollback is not even advisable on this ledger; nothing below would mean anything"
        Assert (-not (Test-StorageFailureObserved -Ledger $dead)) "the decision still calls a dead container a storage failure"

        $out = Run-Summary $dead
        Assert (@($out).Count -gt 6) "the lifted block printed almost nothing ($(@($out).Count) lines); it did not run"
        $iAlarm    = IndexOf $out 'DO NOT RECREATE YET'
        $iMount    = IndexOf $out 'mount-nas-shares.ps1'
        $iRecreate = IndexOf $out '--force-recreate --no-build --pull never'
        Assert ($iAlarm -lt 0) "the wrapper printed a STORAGE alarm for a dead container: '$($out[$iAlarm])'"
        Assert ($iMount -lt 0) "the wrapper sent the operator to mount-nas-shares.ps1 for a dead container"
        Assert ($iRecreate -ge 0) "the wrapper did not print the rollback command at all"
        Assert ((IndexOf $out 'To roll back now') -ge 0) "the rollback was printed without being OFFERED as the next step"
        Assert ((IndexOf $out 'has been REVERTED') -ge 0) "the REVERTED promotion was not reported"
        # The false sentence, quoted, because it is the one the operator acted on.
        Assert ((IndexOf $out 'which is the failure being reported') -lt 0) `
            "the wrapper still tells the operator the reported failure is the mount state"

        # CONTROL A -- the block is not simply dead. A GENUINE storage failure
        # still produces the alarm, and still puts mount-nas-shares.ps1 FIRST.
        $realStorage = DeadFinalLedger @{ nas_final_reason = 'probed'; nas_final_code = 2 }
        $out2 = Run-Summary $realStorage
        $jAlarm    = IndexOf $out2 'DO NOT RECREATE YET'
        $jMount    = IndexOf $out2 'mount-nas-shares.ps1'
        $jRecreate = IndexOf $out2 '--force-recreate --no-build --pull never'
        Assert ($jAlarm -ge 0) "a REAL storage failure no longer warns at all"
        Assert ($jMount -ge 0 -and $jRecreate -ge 0) "a real storage failure printed neither remedy"
        Assert ($jMount -lt $jRecreate) "the recreate is offered BEFORE re-proving the mounts on a real storage failure"

        # CONTROL B -- and the liveness flag is what separates the two. Same
        # ledger as the passing arm except that the run measured the container
        # ALIVE, so 'not-running' is unexplained and the alarm returns.
        $out3 = Run-Summary (DeadFinalLedger @{ final_container_running = $true })
        Assert ((IndexOf $out3 'DO NOT RECREATE YET') -ge 0) `
            "the exemption fires without the run's own liveness measurement backing it"
    }

    # -----------------------------------------------------------------------
    Check "R4-101-1: a FIRST-EVER deploy that fails after promotion says there is NO PRIOR IMAGE" {
        # Deliberately the FIRST Docker case in this file, because it is the
        # only moment at which the fixture genuinely has no previous image --
        # which is the state the wording has to be honest about.
        #
        # The promotion transaction cannot be aborted here: there is nothing to
        # put back. Round 3's runbook said a failed deploy leaves "the old image
        # still on disk and the recovery task knows only that image", and on a
        # first deploy that sentence describes an image that does not exist. So
        # the requirement is not a revert, it is a refusal to pretend: the
        # ledger must say NO PRIOR IMAGE, and the operator must be told the next
        # step is to fix and redeploy rather than to roll back.
        Assert ($null -eq (Get-ImgId $TAG)) "$TAG already exists; this case cannot model a first-ever deploy"
        Assert ($null -eq (Get-CtrId)) "a container already exists; this case cannot model a first-ever deploy"
        try {
            $r = Invoke-Deploy -Hook 'StopAfterReconcile'
            Assert ($r.Exit -ne 0) "a dead final container must exit nonzero; got $($r.Exit); log $($r.Log)"
            Assert ($r.Verdict -eq 'PROBLEMS') "expected PROBLEMS, got $($r.Verdict); log $($r.Log)"
            # The premise, so a later assertion cannot pass for the wrong reason.
            Assert ($null -eq $r.L.recovery_tag_before) "the engine saw a prior image: $($r.L.recovery_tag_before)"
            Assert ([bool](@($r.L.problems) -match 'final container is not running')) `
                "the final container was not what failed: $(@($r.L.problems) -join '; ')"
            # The wording itself.
            Assert ($r.L.promotion_state -like '*NO PRIOR IMAGE*') `
                "the ledger does not say there is no prior image: '$($r.L.promotion_state)'"
            Assert ($r.L.promotion_state -notlike '*REVERTED*') `
                "the ledger claims a revert that cannot have happened: '$($r.L.promotion_state)'"
            # And it is not dressed up: the tag really does name the candidate,
            # and the ledger says so rather than reporting a clean rollback.
            Assert ($r.L.promoted -eq $true) "the ledger denies a promotion that did happen"
            Assert ((Get-ImgId $TAG) -eq (Get-ImgId $r.L.candidate_tag)) `
                "$TAG does not name the candidate, so 'nothing to roll back to' is not the state being described"
        } finally {
            # Leave the fixture as SEED expects to find it: no container, and no
            # $TAG, so the next case is still a genuine first deploy.
            Native { docker rm -f $FXNAME } | Out-Null
            Native { docker image rm -f $TAG } | Out-Null
        }
    }

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
    Check "SR3-7: -WhatIf plans without building, tagging or recreating, and cleans up after itself" {
        # The contract as DOCUMENTED. SR3-7 changed the wording from "changes
        # nothing" to "production-safe", because -WhatIf does fetch and prune
        # git refs and does create and remove a worktree. Both halves are
        # asserted here: the mutations it must never make, and the side effect
        # it now admits to -- including that the worktree is cleaned up, since
        # a dry run that left one registered would make the next REAL deploy of
        # that commit delete something it did not create.
        $beforeCtr  = Get-CtrId
        $beforeTag  = Get-ImgId $TAG
        $beforeImgs = @((Native { docker image ls --format '{{.Repository}}:{{.Tag}}' }).Output | Where-Object { $_ -like "$CAND*" })
        $r = Invoke-Deploy -Extra @{ WhatIf = $true }
        Assert ($r.Exit -eq 0) "a dry run must exit 0; got $($r.Exit): $($r.L.stop_reason); log $($r.Log)"
        Assert ($r.Verdict -eq 'plan only') "expected 'plan only', got $($r.Verdict)"
        Assert ($r.L.build_attempted -ne $true) "-WhatIf attempted a build"
        Assert ($null -eq $r.L.candidate_tag) "-WhatIf named a candidate tag"
        Assert ($null -eq $r.L.activate_exit) "-WhatIf activated something"
        Assert ($r.L.promoted -ne $true) "-WhatIf promoted the recovery tag"
        Assert ((Get-CtrId) -eq $beforeCtr) "-WhatIf recreated the container"
        Assert ((Get-ImgId $TAG) -eq $beforeTag) "-WhatIf moved $TAG"
        $afterImgs = @((Native { docker image ls --format '{{.Repository}}:{{.Tag}}' }).Output | Where-Object { $_ -like "$CAND*" })
        Assert ($afterImgs.Count -eq $beforeImgs.Count) "-WhatIf created a candidate image: $($afterImgs -join ', ')"
        # The plan is only worth anything if it resolved the ref it would
        # deploy -- round 2's dry run reported whatever was checked out.
        Assert ([bool]$r.L.target_sha) "-WhatIf did not resolve the ref it would deploy"
        Assert ([bool]$r.L.source_dir) "-WhatIf did not create a worktree -- the side effect the contract now admits to"
        Assert (-not (Test-Path -LiteralPath $r.L.source_dir)) "-WhatIf left its worktree on disk at $($r.L.source_dir)"
        $wt = (Native { git -C $WORK worktree list }).Text
        Assert ($wt -notmatch 'scanhound-src-') "-WhatIf left a worktree registered: $wt"
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
    Check "CASE A2: a STALE candidate for the same SHA does not let a failed build through" {
        # EVIDENCE-1. CASE A above pins the refusal REASON, which was the
        # minimum needed to kill the build-exit mutant -- but only because the
        # candidate-does-not-exist guard happened to stop the deploy as well.
        # That is a diagnostic string standing in for a safety guard, and it
        # stops working the moment the candidate DOES exist.
        #
        # Candidate tags are deterministic by SHA, so an earlier run of the
        # same commit leaves one behind: the build fails, candidate-exists
        # passes, and with the exit-code check gone the deploy activates a
        # STALE image while reporting the target commit as its provenance.
        #
        # The stale candidate is seeded from the image production is running,
        # which is the WORST case rather than a convenient one: it starts, it
        # serves, it answers /health, and nothing downstream can tell that it
        # is not what was asked for. Every assertion below is therefore a
        # safety OUTCOME, not a message.
        $before    = Get-CtrId
        $beforeTag = Get-ImgId $TAG
        # NOT $cand. That is $CAND, the fixture's candidate PREFIX, because
        # PowerShell variable names are case-insensitive -- see the tripwire in
        # Invoke-Deploy. Writing to it here corrupted every deploy after this
        # case and made this one pass for the wrong reason.
        $staleSha = (Native { git -C $WORK rev-parse origin/main }).Output[0].Trim()
        $staleTag = "$CAND$($staleSha.Substring(0,12))"
        Assert ($null -eq (Get-ImgId $staleTag)) "the fixture already has $staleTag, so seeding it would prove nothing"
        Native { docker tag $beforeTag $staleTag } | Out-Null
        Assert ((Get-ImgId $staleTag) -eq $beforeTag) "the stale candidate was not seeded"
        try {
            $r = Invoke-Deploy
            Assert ($r.Exit -ne 0) "a failed build must exit nonzero even with a stale candidate present; got $($r.Exit)"
            Assert ($r.Verdict -ne 'VERIFIED') "a failed build verified because a stale candidate existed"
            Assert ($r.L.build_exit -ne 0) "the build was supposed to FAIL here; exit $($r.L.build_exit); log $($r.Log)"
            Assert ($null -eq $r.L.activate_exit) "the deploy ACTIVATED after a failed build: activate_exit $($r.L.activate_exit)"
            Assert ($r.L.promoted -ne $true) "the stale candidate was promoted after a failed build"
            Assert ((Get-CtrId) -eq $before) "the container was replaced after a failed build: $before -> $(Get-CtrId)"
            Assert ((Get-Version) -eq 'V2') "the old container is no longer serving V2"
            Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved after a failed build"
            # And the premise, asserted last so a failure above is not masked
            # by it: the deploy really did look for the tag that was seeded.
            Assert ($r.L.candidate_tag -ceq $staleTag) `
                "the engine's candidate was '$($r.L.candidate_tag)', not the seeded '$staleTag' -- this case did not test a stale candidate at all"
        } finally {
            Native { docker image rm -f $staleTag } | Out-Null
        }
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
    Check "SR3-4: a build section this engine would not reproduce is refused before the build" {
        # The guard used to ACCEPT context, dockerfile and dockerfile_inline
        # and RETURN the context, while the engine always built
        #     docker build -t <candidate> -f <clean-root>/Dockerfile <clean-root>
        # Both variants below are lies of that shape, and they are different
        # lies: the wrong TREE and the wrong RECIPE. Either one would have been
        # explicitly accepted and then reported with the target commit's
        # provenance -- the OPS-1 defect in a different place.
        #
        # Both the deployed and the pinned recipe are moved together, or the
        # SR2-1 drift refusal fires first and this case passes for the wrong
        # reason.
        $beforeCtr = Get-CtrId
        $beforeTag = Get-ImgId $TAG
        Assert ($COMPOSE_V1 -match 'build: \.') "the fixture recipe no longer uses a root build; this case would prove nothing"
        try {
            foreach ($variant in @(
                @{ Name  = 'context: ./subdir'
                   Yaml  = ($COMPOSE_V1 -replace 'build: \.', "build:`n      context: ./subdir")
                   Match = '*build context renders as*' },
                @{ Name  = 'args: BUILD_FLAVOUR=slim'
                   Yaml  = ($COMPOSE_V1 -replace 'build: \.', "build:`n      context: .`n      args:`n        BUILD_FLAVOUR: slim")
                   Match = '*uses args*' },
                @{ Name  = 'dockerfile: Dockerfile.production'
                   Yaml  = ($COMPOSE_V1 -replace 'build: \.', "build:`n      context: .`n      dockerfile: Dockerfile.production")
                   Match = '*names dockerfile*' }
            )) {
                Set-TargetVersion -Version 'V5b' -Compose $variant.Yaml
                Set-Content -LiteralPath $PINNED -Value $variant.Yaml -Encoding ASCII
                $r = Invoke-Deploy
                Assert ($r.Exit -ne 0) "$($variant.Name): must exit nonzero; got $($r.Exit)"
                Assert ($r.Verdict -eq 'STOPPED') "$($variant.Name): expected STOPPED, got $($r.Verdict)"
                Assert ($r.L.stop_reason -like $variant.Match) "$($variant.Name): wrong refusal: $($r.L.stop_reason)"
                # Before the build, so an unreviewable build is never paid for.
                Assert ($r.L.build_attempted -ne $true) "$($variant.Name): it built before noticing"
                Assert ((Get-CtrId) -eq $beforeCtr) "$($variant.Name): the container changed despite a refusal"
                Assert ((Get-ImgId $TAG) -eq $beforeTag) "$($variant.Name): $TAG moved despite a refusal"
            }
        } finally {
            # Restored in a finally for the same reason CASE E does it: the
            # first run of that case left the recipe drifted and every later
            # case inherited its refusal instead of testing its own property.
            Set-TargetVersion -Version 'V5c' -Compose $COMPOSE_V1
            Set-Content -LiteralPath $PINNED -Value $COMPOSE_V1 -Encoding ASCII
        }
    }

    # -----------------------------------------------------------------------
    Check "SR3-4: a service-level platform: this engine never passes is refused before the build" {
        # Why the guard looks at the SERVICE and not only at its build section.
        # `docker compose config --format json` renders platform as a SIBLING
        # of build -- measured on this host, the service keys come back as
        # build, command, entrypoint, image, networks, platform, and the build
        # keys as context, dockerfile only -- so a guard that walked $svc.build
        # ACCEPTED it, while `docker compose build --print` resolves the very
        # same file to target.app.platforms. This engine's plain docker build
        # passes no --platform at all, so compose and the engine would produce
        # DIFFERENT images while the ledger reported the target commit's
        # provenance for the engine's one. A false provenance claim: OPS-1's
        # defect wearing a different key.
        #
        # linux/amd64 is deliberate. It is this host's own platform, so the
        # refusal cannot be a build that would have failed anyway.
        $beforeCtr = Get-CtrId
        $beforeTag = Get-ImgId $TAG
        Assert ($COMPOSE_V1 -notmatch 'platform') "the fixture recipe already sets platform; this case would prove nothing"
        # The fragment is spliced with the SAME line ending the document
        # already uses, and the assertion tolerates \r. core.autocrlf is true on
        # this repo, so a fresh `git worktree add` checks this file out with
        # CRLF -- and .NET's (?m)$ matches before \n but NOT before \r\n.
        # Measured: LF -> True, CRLF -> False. The old form therefore passed on
        # a working copy that happened to be LF and FAILED on a clean checkout
        # (33/1, not 34/0). A suite whose verdict moves with the checkout is
        # not a suite.
        $nl = if ($COMPOSE_V1 -match "`r`n") { "`r`n" } else { "`n" }
        $yaml = ($COMPOSE_V1 -replace 'build: \.', ("build: ." + $nl + "    platform: linux/amd64"))
        Assert ($yaml -match '(?m)^    platform: linux/amd64\r?$') "platform was not inserted at SERVICE level; this case would be testing the build section instead"
        try {
            # Both recipes move together, or the SR2-1 drift refusal fires
            # first and this case passes for the wrong reason.
            Set-TargetVersion -Version 'V5d' -Compose $yaml
            Set-Content -LiteralPath $PINNED -Value $yaml -Encoding ASCII
            $r = Invoke-Deploy
            Assert ($r.Exit -ne 0) "a service-level platform must exit nonzero; got $($r.Exit)"
            Assert ($r.Verdict -eq 'STOPPED') "expected STOPPED, got $($r.Verdict)"
            # '*sets platform*', not merely '*platform*': the SERVICE-level
            # branch has to be the one that refused. The build-section
            # allow-list says "uses ...", and crediting this case to that
            # branch is the exact confusion the fix exists to remove.
            Assert ($r.L.stop_reason -like '*sets platform*') "the refusal is not the service-level build-affecting check: $($r.L.stop_reason)"
            # Before the build, so an unreproducible build is never paid for.
            Assert ($r.L.build_attempted -ne $true) "it built before noticing the platform override"
            Assert ((Get-CtrId) -eq $beforeCtr) "the container changed despite a refusal"
            Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved despite a refusal"
        } finally {
            Set-TargetVersion -Version 'V5e' -Compose $COMPOSE_V1
            Set-Content -LiteralPath $PINNED -Value $COMPOSE_V1 -Encoding ASCII
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
        $m = New-Object System.Threading.Mutex($false, $RECMUTEX)
        $held = $false
        try {
            $held = $m.WaitOne(0)
            Assert $held "the test could not take the fixture mutex -- the case would prove nothing"
            $r = Invoke-Deploy -Extra @{ MutexTimeoutSec = 5 }
            Assert ($r.Exit -ne 0) "a held mutex must exit nonzero; got $($r.Exit)"
            Assert ($r.L.stop_reason -like '*RECOVERY lock*') "the refusal does not name the RECOVERY lock: $($r.L.stop_reason)"
            # SR3-5 added a SECOND lock. This case must not be able to pass
            # because of that one: they are taken at different moments, for
            # different spans, against different contenders.
            Assert ($r.L.stop_reason -notlike '*deploy-instance*') "this case contended the recovery lock but the deploy-instance lock refused: $($r.L.stop_reason)"
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
    Check "SR3-5: a second deploy refuses while the first holds the DEPLOY-INSTANCE lock" {
        # Deliberately confusable with CASE G, and deliberately proven not to
        # be. CASE G contends the RECOVERY lock, which is taken late and only
        # around the container transition -- so its refusal arrives AFTER a
        # full build. This lock is taken in section 1 and held for the whole
        # run, so its refusal must arrive before anything is fetched, resolved
        # or materialised.
        #
        # Two deploys of the same commit do not race occasionally: every name
        # they derive is a function of the SHA -- the worktree, the candidate
        # tag, the override file -- and New-CleanSource DELETES a pre-existing
        # worktree of that name before creating its own. The second deploy
        # would take the first one's build source out from under it.
        $beforeCtr = Get-CtrId
        $beforeTag = Get-ImgId $TAG
        $m = New-Object System.Threading.Mutex($false, $DEPLOYMUTEX)
        $held = $false
        try {
            $held = $m.WaitOne(0)
            Assert $held "the test could not take the fixture deploy lock -- the case would prove nothing"
            $r = Invoke-Deploy
            Assert ($r.Exit -ne 0) "a second concurrent deploy must exit nonzero; got $($r.Exit)"
            Assert ($r.Verdict -eq 'STOPPED') "expected STOPPED, got $($r.Verdict)"
            Assert ($r.L.stop_reason -like '*deploy-instance*') "the refusal is not the deploy-instance lock: $($r.L.stop_reason)"
            Assert ($r.L.stop_reason -notlike '*RECOVERY lock*') "the RECOVERY lock refused instead: $($r.L.stop_reason)"
            # The separation from CASE G, asserted rather than assumed.
            Assert ($r.L.build_attempted -ne $true) "the second deploy built before refusing"
            Assert ($null -eq $r.L.target_sha) "the second deploy resolved a target before refusing"
            Assert ($null -eq $r.L.source_dir) "the second deploy created a worktree -- which is what would delete the first deploy's source"
            Assert ((Get-CtrId) -eq $beforeCtr) "the container changed while another deploy held the lock"
            Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved while another deploy held the lock"
        } finally {
            if ($held) { $m.ReleaseMutex() }
            $m.Dispose()
        }
    }

    # -----------------------------------------------------------------------
    Check "SR3-5: the deploy-instance lock is still HELD late in the run, not merely taken" {
        # THE HONESTY FIX for the case above. That one can only see that the
        # engine ASKS for the lock: the TEST holds it, so the engine never gets
        # past section 1. When the ENGINE lets go is invisible from there, and
        # a mutation run proved exactly that -- releasing the deploy mutex on
        # the line immediately after taking it left the suite at 23 passed.
        #
        # So this case asks from the other side. A hook inside a run that is
        # still in progress spawns a SEPARATE process which tries to take the
        # same named mutex, and that attempt must FAIL.
        #
        # A separate PROCESS is not incidental. A named mutex is re-entrant for
        # the thread that already owns it, so a WaitOne issued from inside the
        # deploy process would succeed while the lock is held and the case
        # would assert the opposite of what it means.
        #
        # Nothing here is timing dependent, which is why this was written
        # rather than the gap declared: the probe runs SYNCHRONOUSLY from
        # inside the engine's own call stack, at two points that are both far
        # past section 1 -- after the container is activated, and again after
        # the post-promotion reconcile.
        $probe = Join-Path $FX "lockprobe-$([guid]::NewGuid().ToString('N').Substring(0,6)).txt"
        Set-TargetVersion -Version 'V6a'
        $r = Invoke-Deploy -Hook 'ProbeDeployLock' -HookArg $probe
        Assert ($r.Exit -eq 0) "expected a clean deploy, got exit $($r.Exit): $($r.L.stop_reason); log $($r.Log)"
        Assert ($r.Verdict -eq 'VERIFIED') "expected VERIFIED, got $($r.Verdict)"
        Assert (Test-Path -LiteralPath $probe) "the lock probe never ran, so this case measured nothing; log $($r.Log)"
        $lines = @(Get-Content -LiteralPath $probe | Where-Object { $_ -match '\S' })
        Assert ($lines.Count -eq 2) "expected two probe answers, got $($lines.Count): $($lines -join '; ')"
        Assert ($lines -contains 'activate=BLOCKED') "another process took the deploy lock while a deploy was mid-run: $($lines -join '; ')"
        Assert ($lines -contains 'reconcile=BLOCKED') "another process took the deploy lock after the reconcile: $($lines -join '; ')"

        # THE CONTROL, and it is the whole case. Two BLOCKED answers prove
        # nothing unless this probe is CAPABLE of saying ACQUIRED: a mistyped
        # name, a failed spawn, or a probe that always answered BLOCKED would
        # read exactly like a held lock. The deploy has exited, so the SAME
        # script against the SAME mutex name must now come back ACQUIRED.
        $probeScript = "$probe.probe.ps1"
        Assert (Test-Path -LiteralPath $probeScript) "the probe script is missing at $probeScript"
        $after = (Native { powershell -NoProfile -ExecutionPolicy Bypass -File $probeScript -Name $DEPLOYMUTEX }).Text
        Assert ($after -match 'ACQUIRED') `
            "the probe cannot report ACQUIRED even with the deploy finished, so its BLOCKED answers are worthless: $after"
    }

    # -----------------------------------------------------------------------
    Check "SR3-6: the rollback offer is driven by the OBSERVER, not by new_container_id" {
        # A pure decision, so it is exercised directly instead of through
        # Docker: the state it has to get right -- Compose partially replaced
        # the container and THEN returned nonzero, leaving section 6 unreached
        # -- is a Compose failure mode this fixture cannot induce on demand.
        # What can be pinned exactly is the decision itself, including the row
        # that used to come out wrong.
        . (Join-Path (Split-Path -Parent $PSScriptRoot) 'scripts\deploy-core.ps1')

        function L([hashtable]$Over) {
            $base = @{
                promoted         = $false
                old_container_id = 'aaaaaaaaaaaa'
                new_container_id = $null
                observed         = @{ container_id = 'UNKNOWN'; running = 'UNKNOWN' }
            }
            foreach ($k in $Over.Keys) { $base[$k] = $Over[$k] }
            return $base
        }

        # THE FINDING. Section 6 never ran, so new_container_id is null -- and
        # the observer, which measures AFTER the failure, plainly reports a
        # different container running. The old condition answered "no rollback"
        # in exactly the case the operator most needs one.
        Assert (Test-RollbackAdvisable -Ledger (L @{ observed = @{ container_id = 'bbbbbbbbbbbb'; running = 'true' } })) `
            "a container replaced without new_container_id being written is the case the rollback exists for"
        # Replaced and then died is still replaced.
        Assert (Test-RollbackAdvisable -Ledger (L @{ observed = @{ container_id = 'bbbbbbbbbbbb'; running = 'false' } })) `
            "a replaced-but-dead container still needs the rollback"
        # First deploy: nothing ran before, something unverified runs now.
        Assert (Test-RollbackAdvisable -Ledger (L @{ old_container_id = $null; observed = @{ container_id = 'bbbbbbbbbbbb'; running = 'true' } })) `
            "a first deploy still left an unverified container running"
        # Nothing was replaced.
        Assert (-not (Test-RollbackAdvisable -Ledger (L @{ observed = @{ container_id = 'aaaaaaaaaaaa'; running = 'true' } }))) `
            "the same container is still running; there is nothing to roll back"
        # Promoted: the pinned recipe would recreate the SAME image, so that
        # command rolls nothing back and offering it would mislead.
        Assert (-not (Test-RollbackAdvisable -Ledger (L @{ promoted = $true; observed = @{ container_id = 'bbbbbbbbbbbb'; running = 'true' } }))) `
            "the recovery tag was promoted; that command would not roll anything back"
        # R4-101-1. `promoted` is now the CURRENT state of the tag, not a record
        # that a promotion once happened -- and these two rows are why. A
        # promotion that was REVERTED leaves the tag on the prior image, so the
        # recreate genuinely is a rollback and must be OFFERED; a revert that
        # FAILED leaves the tag on the candidate, so it must not be. Reading a
        # history flag here would get both of them backwards.
        Assert (Test-RollbackAdvisable -Ledger (L @{
            promoted = $false
            promotion_state = 'promoted, then REVERTED to the prior image'
            observed = @{ container_id = 'bbbbbbbbbbbb'; running = 'true' } })) `
            "a REVERTED promotion leaves the tag on the prior image; the rollback must still be offered"
        Assert (-not (Test-RollbackAdvisable -Ledger (L @{
            promoted = $true
            promotion_state = 'promoted; the REVERT FAILED'
            observed = @{ container_id = 'bbbbbbbbbbbb'; running = 'true' } }))) `
            "the tag still names the candidate, so that command would not roll anything back"
        # An observation that could not be made is not evidence of replacement.
        foreach ($u in @('UNKNOWN', 'ABSENT', '')) {
            Assert (-not (Test-RollbackAdvisable -Ledger (L @{ observed = @{ container_id = $u; running = 'UNKNOWN' } }))) `
                "'$u' was treated as a container id"
        }
        Assert (-not (Test-RollbackAdvisable -Ledger @{ promoted = $false; old_container_id = 'aaaaaaaaaaaa'; observed = $null })) `
            "a missing observation was treated as evidence of a replacement"
        # And the SHAPE the wrapper actually receives. The ledger reaches the
        # operator-facing switch after a JSON round trip in the fixture and as
        # an ordered hashtable in production; a decision that only works on one
        # of those is a decision that works in the test and not in the field.
        $viaJson = (L @{ observed = @{ container_id = 'bbbbbbbbbbbb'; running = 'true' } }) | ConvertTo-Json -Depth 5 | ConvertFrom-Json
        Assert (Test-RollbackAdvisable -Ledger $viaJson) "the decision does not survive the JSON round trip the ledger takes"
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

    # =======================================================================
    # SR3-1 / SR3-2. Everything below runs with the storage proofs ENABLED and
    # a compose recipe that actually binds something, because a proof that only
    # ever sees a correct system has not been shown to fail.
    # =======================================================================

    function Set-NasRecipe {
        <# Move BOTH the deployed recipe and the pinned recovery recipe, or the
           SR2-1 drift refusal fires first and every case below would pass for
           the wrong reason. #>
        param([string]$Version, [string]$Compose)
        Set-TargetVersion -Version $Version -Compose $Compose
        Set-Content -LiteralPath $PINNED -Value $Compose -Encoding ASCII
    }

    # -----------------------------------------------------------------------
    Check "SR3-2: the reconcile recreates the container and the FINAL container is the one checked" {
        # The reviewer's second blocker. The candidate is qualified, the image
        # is promoted, plain Compose runs again -- and round 3 then checked
        # container-inspect success and image-id equality only. This case pins
        # that the cheap checks run a SECOND time, against whatever that
        # reconcile leaves running.
        Native { docker rm -f $FXNAME } | Out-Null
        Set-NasRecipe -Version 'V7' -Compose (New-NasCompose)
        $r = Invoke-Deploy -Extra (New-NasConfig)
        Assert ($r.Exit -eq 0) "expected VERIFIED, got exit $($r.Exit): $($r.L.stop_reason); log $($r.Log)"
        Assert ($r.Verdict -eq 'VERIFIED') "expected VERIFIED, got $($r.Verdict)"
        Assert ($r.L.nas_host_code -eq 0) "the host storage proof did not pass: code $($r.L.nas_host_code) reason $($r.L.nas_host_reason)"
        Assert ($r.L.nas_candidate_code -eq 0) "the candidate storage proof did not pass: $($r.L.nas_candidate_reason)/$($r.L.nas_candidate_code)"
        Assert ($r.L.nas_final_code -eq 0) "the FINAL storage proof did not run or did not pass: $($r.L.nas_final_reason)/$($r.L.nas_final_code)"
        # The case is only load bearing if the reconcile really did replace the
        # container -- otherwise "the final checks ran against the new one" is
        # trivially true. Assert the premise, do not assume it.
        Assert ($r.L.reconcile_recreated -eq $true) "the reconcile did NOT recreate the container, so this case proves nothing about the final one"
        Assert ([bool]$r.L.candidate_container_id) "no candidate container id was recorded"
        Assert ([bool]$r.L.final_checks_container_id) "the final checks recorded no container id -- they did not run"
        Assert ($r.L.final_checks_container_id -ne $r.L.candidate_container_id) `
            "the final checks ran against the candidate container $($r.L.candidate_container_id), not the one the reconcile created"
        Assert ($r.L.final_checks_container_id -eq (Get-CtrId)) `
            "the final checks ran against $($r.L.final_checks_container_id) but $(Get-CtrId) is what is running"
        Assert ((Get-Version) -eq 'V7') "the running service does not report V7"
    }

    # -----------------------------------------------------------------------
    Check "SR3-2: a post-reconcile container that is not serving is refused despite the correct image" {
        # The exact hole: image id correct, everything else broken. The hook
        # stops the container AFTER the reconcile, so the engine's own
        # image-identity check still passes and only the final runtime checks
        # can catch it.
        $r = Invoke-Deploy -Extra (New-NasConfig) -Hook 'StopAfterReconcile'
        Assert ($r.Exit -ne 0) "a dead final container must exit nonzero; got $($r.Exit)"
        Assert ($r.Verdict -eq 'PROBLEMS') "expected PROBLEMS, got $($r.Verdict)"
        Assert ($r.L.new_image_id -eq $r.L.built_image_id) `
            "the image id already differed, so this case would not be about the FINAL container"
        Assert ([bool]$r.L.final_checks_container_id) "the final checks did not run at all"
        Assert ([bool](@($r.L.problems) -match 'final container is not running')) `
            "the final runtime state was not asserted: $(@($r.L.problems) -join '; ')"
        # A storage proof that could not RUN is UNKNOWN, never a pass. Pinned
        # here because "the probe did not answer" is the exact shape the
        # 2026-07-26 outage took, and a deploy that shrugs at it is the deploy
        # that shipped it.
        Assert ([bool](@($r.L.unknown) -match 'could not be probed')) `
            "an unrunnable storage probe was not reported as UNKNOWN: $(@($r.L.unknown) -join '; ')"
        Assert ($r.L.nas_final_reason -eq 'not-running') "the final storage probe reason was '$($r.L.nas_final_reason)'"
        # R4-101-1. This case used to assert `promoted -eq $true` and call that
        # honesty: the image had qualified, so the tag was moved and left there.
        # The reviewer's point is that leaving it there breaks the recovery
        # contract -- NOT VERIFIED with the recovery tag on the NEW image means
        # a recovery recreate re-creates the image this run just failed to
        # qualify. So the promotion is provisional and is REVERTED here, and
        # the ledger has to distinguish that from never having been promoted.
        Assert ($r.L.promotion_state -like '*REVERTED*') `
            "the ledger does not record a promotion that was made and then reverted: '$($r.L.promotion_state)'"
        Assert ($r.L.promotion_state -notlike '*REVERT FAILED*') "the revert did not take: '$($r.L.promotion_state)'"
        Assert ($r.L.promoted -eq $false) "$TAG is still promoted to an image whose final container failed"
        Native { docker rm -f $FXNAME } | Out-Null
    }

    # -----------------------------------------------------------------------
    Check "SR3-1: a host source that is not the expected share refuses BEFORE activation" {
        # The reviewer's first blocker, at the point where it matters most:
        # holding the shared mutex, the deploy is the only actor able to touch
        # the container, and recreating it against an unproven source is what
        # binds /library/tv to a local directory.
        Set-NasRecipe -Version 'V8' -Compose (New-NasCompose)
        $before    = Get-CtrId
        $beforeTag = Get-ImgId $TAG
        # Everything is correct EXCEPT where the read-write destination is
        # sourced from -- an ordinary local directory standing where the share
        # should be.
        $r = Invoke-Deploy -Extra (New-NasConfig -TvSource $VOLDECOY)
        Assert ($r.Exit -ne 0) "an unproven host source must exit nonzero; got $($r.Exit)"
        Assert ($r.Verdict -eq 'STOPPED') "expected STOPPED, got $($r.Verdict)"
        Assert ($r.L.nas_host_reason -eq 'probed') "the host probe did not run: $($r.L.nas_host_reason)"
        Assert ($r.L.nas_host_code -eq 2) "the CRITICAL source failure should be probe exit 2, got $($r.L.nas_host_code)"
        Assert ($r.L.stop_reason -like '*host storage sources are NOT*') "the refusal is not the host storage proof: $($r.L.stop_reason)"
        Assert ($r.L.build_attempted -eq $true) "the proof is supposed to run after the build, inside the mutex"
        Assert ($null -eq $r.L.activate_exit) "the container was activated despite an unproven source"
        Assert ((Get-CtrId) -eq $before) "the container was replaced against an unproven source: $before -> $(Get-CtrId)"
        Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved despite an unproven source"
    }

    # -----------------------------------------------------------------------
    Check "SR3-1: a host proof that cannot be obtained refuses instead of passing" {
        # UNKNOWN is not OK. The 2026-07-26 outage was invisible precisely
        # because a measurement that never happened read as a clean result, so
        # a probe the engine could not run must stop the deploy exactly as a
        # failed one does.
        $before    = Get-CtrId
        $beforeTag = Get-ImgId $TAG
        $cfg = New-NasConfig
        # --pull never is already passed, so an image that does not exist
        # locally makes the throwaway probe container fail to start. Nothing is
        # fetched from a registry.
        $cfg['NasHostProbeImage'] = "${FXNAME}-no-such-probe-image:none"
        $r = Invoke-Deploy -Extra $cfg
        Assert ($r.Exit -ne 0) "an unobtainable host proof must exit nonzero; got $($r.Exit)"
        Assert ($r.Verdict -eq 'STOPPED') "expected STOPPED, got $($r.Verdict)"
        Assert ($r.L.nas_host_reason -eq 'host-container-failed') "unexpected probe reason: $($r.L.nas_host_reason)"
        Assert ($r.L.stop_reason -like '*could not be probed*') "the refusal is not the unmeasurable-proof guard: $($r.L.stop_reason)"
        Assert ($null -eq $r.L.activate_exit) "the container was activated without a storage proof"
        Assert ((Get-CtrId) -eq $before) "the container was replaced without a storage proof"
        Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved without a storage proof"
    }

    # -----------------------------------------------------------------------
    Check "SR3-1: a container bind that resolves somewhere else is refused after activation" {
        # Compose exit 0 proves a container started. Here it starts perfectly
        # and /library/tv is a different filesystem than the one that was
        # proven on the host side -- which is precisely what happens when the
        # WSL2 mount is absent at container-create time.
        $beforeTag = Get-ImgId $TAG
        Set-NasRecipe -Version 'V9' -Compose (New-NasCompose -TvVolume $VOLDECOY)
        $r = Invoke-Deploy -Extra (New-NasConfig)
        Assert ($r.Exit -ne 0) "a wrong bind mount must exit nonzero; got $($r.Exit)"
        Assert ($r.Verdict -eq 'PROBLEMS') "expected PROBLEMS, got $($r.Verdict)"
        # The host sources were fine. That separation is the case: without the
        # container-side proof this deploy is completely green.
        Assert ($r.L.nas_host_code -eq 0) "the host proof was supposed to PASS here; got $($r.L.nas_host_code)"
        Assert ($r.L.nas_candidate_code -eq 2) "the candidate container proof did not report a critical failure: $($r.L.nas_candidate_code)"
        Assert ([bool](@($r.L.problems) -match 'NOT the intended shares')) "the bind identity was not asserted: $(@($r.L.problems) -join '; ')"
        Assert ($r.L.promoted -ne $true) "promoted despite bind mounts pointing somewhere else"
        Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved despite bind mounts pointing somewhere else"
    }

    # -----------------------------------------------------------------------
    Check "SR3-1: a critical destination that is present but not writable is refused" {
        # Identity passes -- same volume, same origin, same filesystem type --
        # and the deploy must still refuse, because /library/tv is a download,
        # extraction and rename DESTINATION. "Mounted" is not "writable", and a
        # read-only or stale-handle mount breaks every TV rename silently.
        $beforeTag = Get-ImgId $TAG
        Set-NasRecipe -Version 'V10' -Compose (New-NasCompose -TvMode 'ro')
        $r = Invoke-Deploy -Extra (New-NasConfig)
        Assert ($r.Exit -ne 0) "an unwritable destination must exit nonzero; got $($r.Exit)"
        Assert ($r.Verdict -eq 'PROBLEMS') "expected PROBLEMS, got $($r.Verdict)"
        Assert ($r.L.nas_host_code -eq 0) "the host proof was supposed to PASS here; got $($r.L.nas_host_code)"
        Assert ($r.L.nas_candidate_code -eq 2) "the write/delete probe did not report a critical failure: $($r.L.nas_candidate_code)"
        Assert ([bool](@($r.L.problems) -match 'UNWRITABLE')) `
            "the refusal is not the write/delete probe -- identity alone would have passed here: $(@($r.L.problems) -join '; ')"
        Assert ($r.L.promoted -ne $true) "promoted despite an unwritable TV destination"
        Assert ((Get-ImgId $TAG) -eq $beforeTag) "$TAG moved despite an unwritable TV destination"
    }

    # -----------------------------------------------------------------------
    Check "R4-101-2: the promotion journal is OPEN inside the transaction, cleared when it closes, and REPORTED if a previous run left one" {
        # S4. Invoke-PromotionRevert closes the transaction on every exit from
        # the try block. What it cannot close is a run that reaches none of
        # them -- Ctrl+C, a killed window, a reboot. Between the tag move and
        # the revert, scanhound:latest names an UNQUALIFIED image and the ledger
        # has not been written at all, because it is only returned at the end.
        # And scripts/mount-nas-shares.ps1 takes the recovery mutex with
        # WaitOne(0) and CATCHES AbandonedMutexException, so the next recovery
        # pass recreates the container onto that image.
        #
        # This case pins all three halves of the journal in ONE run, because
        # they are one transaction:
        #   1. a journal left by a PREVIOUS run is found and reported;
        #   2. this run's own journal is present WHILE the promotion is open,
        #      observed at the OnAfterReconcile seam by a hook rather than
        #      inferred afterwards;
        #   3. it is gone once the revert has put the prior image back.
        Native { docker rm -f $FXNAME } | Out-Null
        Set-NasRecipe -Version 'V10' -Compose $COMPOSE_V1
        $journal = Join-Path $PINDIR 'promotion-in-flight.json'
        $probe   = Join-Path $FX "journal-probe-$([guid]::NewGuid().ToString('N').Substring(0,6)).txt"
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue

        # (1) A journal from a run that died mid-promotion. Planted rather than
        # produced, because killing the engine at that exact instruction is not
        # something this fixture can do on demand -- and the property under test
        # is what the NEXT run does about it.
        @{ image_tag = $TAG; prior_image = 'sha256:deadbeefdeadbeef'
           candidate_image = 'sha256:cafecafecafecafe'; target_sha = 'f' * 40
           opened_utc = '2026-08-27T04:05:06.0000000Z'; pid = 4242 } |
            ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $journal -Encoding UTF8
        Assert (Test-Path -LiteralPath $journal) "the planted journal was not written; this case would prove nothing"

        $r = Invoke-Deploy -Hook 'ProbeJournalThenStop' -HookArg $probe

        # The premises. This has to be a run that PROMOTED and then reverted,
        # or the journal was never opened and every assertion below is vacuous.
        Assert ($r.Exit -ne 0) "a dead final container must exit nonzero; got $($r.Exit); log $($r.Log)"
        Assert ([bool]$r.L.final_checks_container_id) "the FINAL checks never ran, so no promotion was reached"
        Assert ($r.L.promotion_state -like '*REVERTED*') `
            "this run did not promote-then-revert, so the journal was never opened: '$($r.L.promotion_state)'"

        # (1) reported, and reported with the DEAD run's numbers, not this one's.
        Assert ([bool]$r.L.interrupted_prior_promotion) `
            "a journal left by an interrupted run was not reported at all"
        Assert ($r.L.interrupted_prior_promotion -like '*sha256:deadbeefdeadbeef*') `
            "the report does not name the image the interrupted run left behind: '$($r.L.interrupted_prior_promotion)'"
        Assert ($r.L.interrupted_prior_promotion -like '*2026-08-27T04:05:06*') `
            "the report does not say WHEN the interrupted run opened it: '$($r.L.interrupted_prior_promotion)'"

        # (2) open DURING the transaction, seen from inside the run.
        Assert (Test-Path -LiteralPath $probe) "the OnAfterReconcile hook never wrote its observation"
        $seen = (Get-Content -LiteralPath $probe -Raw).Trim()
        Write-Host "        at OnAfterReconcile: $($seen.Substring(0, [Math]::Min(140, $seen.Length)))"
        Assert ($seen -like 'PRESENT*') `
            ("the promotion journal was $seen while the tag was provisionally promoted. A run " +
             "killed at that moment would leave $TAG on an unqualified image with nothing on " +
             "disk saying so, and the recovery task treats an abandoned mutex as acquired.")
        Assert ($seen -like "*$($r.L.recovery_tag_before)*") `
            "the open journal does not name the image to put back ($($r.L.recovery_tag_before)): $seen"
        Assert ($seen -notlike '*sha256:deadbeefdeadbeef*') `
            "the journal still holds the PLANTED content; this run never wrote its own"

        # (3) closed once the tag is settled -- and settled is what makes it
        # safe to close, so assert the tag really is back on the prior image.
        Assert ((Get-ImgId $TAG) -eq $r.L.recovery_tag_before) `
            "the revert did not put the prior image back, so a cleared journal would be a lie"
        Assert (-not (Test-Path -LiteralPath $journal)) `
            "the journal is STILL on disk after a completed revert; the next run would report a promotion that is already closed"
        Assert ($r.L.promotion_journal -eq 'closed') `
            "the ledger does not record the journal as closed: '$($r.L.promotion_journal)'"
        Native { docker rm -f $FXNAME } | Out-Null
    }

    # -----------------------------------------------------------------------
    Check "the fixture can be returned to a verified state afterwards" {
        Native { docker rm -f $FXNAME } | Out-Null
        Set-NasRecipe -Version 'V11' -Compose $COMPOSE_V1
        $r = Invoke-Deploy
        Assert ($r.Exit -eq 0) "the fixture could not be returned to VERIFIED: $($r.L.stop_reason); log $($r.Log)"
        Assert ((Get-Version) -eq 'V11') "the restored service does not report V11"
    }

    # -----------------------------------------------------------------------
    Check "R4-101-1: a FINAL-qualification failure REVERTS the promotion, and the recovery recipe restores image A" {
        # THE REGRESSION, in the reviewer's own terms. Old latest = image A;
        # candidate = image B; B's own checks pass, so latest temporarily
        # BECOMES B; the final plain-recipe activation leaves a container that
        # fails instance-level qualification; the verdict is not VERIFIED --
        # and latest must be A again.
        #
        # Round 3 left it on B, and argued for it: demoting "would leave the
        # recovery task ready to recreate an OLDER image than the one now
        # running". Recreating the older image is what rollback IS. Left on B,
        # the operator has a NOT VERIFIED verdict and a recovery task that
        # recreates the unqualified image, which is not the contract the
        # runbook gave them.
        #
        # Deliberately NOT a storage case: the NAS proofs are off here, so the
        # only thing that fails is the final container. The SR3-2 case above
        # covers the same transaction with the probes enabled.
        #
        # Placed last because it needs a real prior image on disk, and because
        # the final assertion EXECUTES the recovery recipe -- the fixture is
        # deliberately left rolled back to V11.
        $imgA = Get-ImgId $TAG
        Assert ([bool]$imgA) "there is no prior image A; this case would prove nothing"
        Assert ((Get-Version) -eq 'V11') "the fixture is not serving V11 at the start of this case"

        Set-TargetVersion -Version 'V12'
        $r = Invoke-Deploy -Hook 'StopAfterReconcile'

        # The premises, asserted before the conclusion. 'latest == A' is worth
        # nothing unless B exists, is a different image, and really was
        # promoted over A first.
        $imgB = Get-ImgId $r.L.candidate_tag
        Assert ([bool]$imgB) "the candidate image does not exist; nothing was built to promote"
        Assert ($imgB -ne $imgA) "the candidate is the SAME image as the prior one; this case would prove nothing"
        Assert ($r.L.recovery_tag_before -eq $imgA) "the engine's pre-build tag was not image A: $($r.L.recovery_tag_before)"
        Assert ([bool]$r.L.candidate_container_id) "the candidate checks never ran, so no promotion was reached"
        Assert ([bool]$r.L.final_checks_container_id) "the FINAL checks never ran, so this is not a final-qualification failure"
        Assert ([bool](@($r.L.problems) -match 'final container is not running')) `
            "the final container was not what failed: $(@($r.L.problems) -join '; ')"

        # The verdict.
        Assert ($r.Exit -ne 0) "a failed final qualification must exit nonzero; got $($r.Exit); log $($r.Log)"
        Assert ($r.Verdict -ne 'VERIFIED') "a failed final qualification must not verify"

        # THE ASSERTION.
        Assert ((Get-ImgId $TAG) -eq $imgA) `
            "$TAG is $(Get-ImgId $TAG), not the prior image $imgA -- the promotion was NOT reverted"
        Assert ($r.L.promotion_state -like '*REVERTED*') `
            "the ledger does not record a REVERTED promotion: '$($r.L.promotion_state)'"
        Assert ($r.L.promotion_state -notlike '*REVERT FAILED*') "the revert did not take: '$($r.L.promotion_state)'"
        Assert ($r.L.promoted -eq $false) `
            "the ledger still calls the tag promoted, which is what suppresses the wrapper's rollback offer"
        Assert ($r.L.recovery_tag_after -eq $imgA) "the ledger's post-run tag is not image A: $($r.L.recovery_tag_after)"

        # ORDERING, as far as this fixture can pin it. The reviewer's
        # transaction requires the revert to happen while the recovery mutex is
        # still held, and there is no seam between the revert and the release
        # to probe from. What CAN be shown: Observe-CurrentContainerState runs
        # in the finally, ahead of the mutex release, and it MEASURES the tag
        # rather than replaying a variable. So an observed recovery_tag of
        # image A proves the revert had already happened by then -- which is
        # before the lock was let go. It is truncated to 19 characters there.
        Assert ($r.L.observed.recovery_tag -eq $imgA.Substring(0, 19)) `
            ("the observer, which runs before the recovery lock is released, still saw " +
             "$($r.L.observed.recovery_tag) -- the revert had not happened yet")

        # And the part that makes this a ROLLBACK rather than a tag edit: run
        # the recovery recipe the operator is actually handed -- the same
        # command scripts/mount-nas-shares.ps1 runs -- and prove what it
        # restores. Without this the case would only show that a string in the
        # ledger changed.
        Native { docker compose -f $PINNED --project-directory $WORK up -d --force-recreate --no-build --pull never } | Out-Null
        Assert ((Get-CtrImg) -eq $imgA) "the recovery recipe activated $(Get-CtrImg), not the prior image $imgA"
        Assert ((Get-Version) -eq 'V11') "the recovered service does not report V11 -- the last VERIFIED version"
    }
}
finally {
    Remove-Fixture
}

Write-Host $INVARIANTS
Write-Host ""
Write-Host ("== {0} passed, {1} failed" -f $PASS, $FAIL) -ForegroundColor $(if ($FAIL) { 'Red' } else { 'Green' })
foreach ($f in $FAILED) { Write-Host "   FAILED: $f" -ForegroundColor Red }
exit $(if ($FAIL) { 1 } else { 0 })
