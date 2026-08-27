<#
.SYNOPSIS
    The deploy engine. Parameterised so it can be executed against a disposable
    Docker fixture instead of production.

.DESCRIPTION
    Round 3, 2026-08-26. Round 2 replaced the deploy script and the reviewer's
    exact-code pass found three holes in the replacement BEFORE any of it had
    run. All three were confirmed here against the real repository:

    OPS-1  The build context is not the Git tree.
           `git status --porcelain` filtered to tracked files cannot see
           git-IGNORED content, and Docker does not care what Git tracks.
           Measured on this machine 2026-08-26: frontend/src-tauri/gen is
           1.7 GB, git-ignored, absent from .dockerignore, and inside
           `COPY frontend/ ./frontend/`. A stray chrome.exe sat in the repo
           root as well. "HEAD == T" therefore did NOT imply "build context
           == T", and the ledger's expected_sha was a false provenance claim.
           Fixed by building from a disposable `git worktree`, which also
           removes any need to move the operator off their branch.

    OPS-2  The build promoted the recovery identity before verification.
           docker-compose.yml declares `image: scanhound:latest`, so a
           successful `docker compose build` retagged latest -> NEW before
           activation was attempted. scripts/mount-nas-shares.ps1 (Boot +
           Logon + 288x/day) independently recreates the container with
           `up -d --force-recreate --no-build --pull never`, whose whole
           notion of "what was already reviewed and built" is that mutable
           tag. A failed deploy therefore left an unverified candidate queued
           for delayed automatic activation. Fixed by building under a
           candidate tag and promoting only after VERIFIED, under the same
           named mutex the recovery task uses.

    SR2-1  Compose drift was checked against the wrong tree.
           The pinned-vs-worktree comparison ran in section 1, before PRs
           merged and before the source changed. A PR that edits
           docker-compose.yml therefore deployed cleanly while the pinned
           recovery recipe went stale -- which is the exact 2026-08-11/12
           outage this guard exists to prevent. Fixed by comparing the pinned
           recipe against the TARGET recipe, twice: once after the source is
           materialised, and again immediately before activation.

    OPS-5  Printing the ledger is not observing production.
           On a mid-activation failure the ledger's container fields were
           still null, because the section that fills them had not run. The
           operator got "activate_exit = 1" and no answer to "what is running
           right now?". Fixed with Observe-CurrentContainerState, which cannot
           throw and cannot change state, called from a real finally.

    SR3-1  Production storage identity was outside the proof entirely.
           This file contained zero references to mountpoint, 9p or
           /library/tv. docker-compose.yml binds the WSL2 path
           /mnt/nas/nas-tv-blackbeard to /library/tv READ-WRITE -- the TV
           download, extract and rename DESTINATION -- and those WSL2 mounts do
           not survive a Docker Desktop, WSL or host restart on their own. If
           the mount is absent when the container is created, the bind resolves
           to an ordinary directory inside the VM. Image id, running state,
           port, env, /health and log volume ALL pass in that state, and the
           application writes TV files where Plex will never see them. It has
           happened: 2026-07-26, nine shares unmounted, Scheduled Task
           reporting success.

           Round 3 made this worse rather than better. The shared mutex it
           added means that while a deploy holds the lock, the actor that
           normally enforces mount safety -- ScanHound-MountNASShares -- cannot
           recreate or repair. So the deploy path has to carry the proof
           itself. It does, via scripts/nas-probe.ps1, which LIFTS the identity
           rule out of mount-nas-shares.ps1 rather than restating it.

    SR3-2  The final container was not the one that was qualified.
           The candidate was qualified thoroughly and then the image was
           promoted and plain Compose ran again to drop the candidate-image
           override -- a step this file explicitly allows to recreate the
           container. After it, only container-inspect success and image-id
           equality were checked, and then the verdict was VERIFIED. A
           recreated container can carry the right image and still fail every
           instance-level property: process, port, environment, health, and
           bind mounts. Fixed by treating the reconcile as the FINAL
           ACTIVATION: one Invoke-RuntimeChecks function is called twice, once
           against the candidate and once against whatever the reconcile
           leaves running.

    SR3-4  The build guard accepted semantics the engine ignores.
           Assert-BuildIsPlain permitted context, dockerfile and
           dockerfile_inline and RETURNED the context -- while the engine
           always built `docker build -f <clean-root>/Dockerfile <clean-root>`,
           ignoring every one of them. Production was safe only because
           `build: .` happens to mean the root and the default Dockerfile. A
           reviewed change to `context: ./subdir` would have been explicitly
           ACCEPTED by the guard while the engine built a different tree and
           the ledger claimed the target commit's provenance for it. Fixed by
           making the guard's contract exactly what the engine does: root
           context, default Dockerfile, nothing else, and no return value.

    SR3-5  Two deploys could run at once.
           The recovery mutex is taken late on purpose -- a ten-minute build
           must not block mount recovery -- which left deploy-vs-deploy
           unserialised. Everything derived from the target SHA is
           deterministic (worktree scanhound-src-<sha12>, candidate tag
           <prefix>candidate-<sha12>, override scanhound-candidate-<sha12>.yml)
           and New-CleanSource REMOVES a pre-existing worktree of that name, so
           a second deploy deletes the first one's build source mid-build.
           Fixed with a SECOND, whole-run deploy-instance mutex. The two are
           deliberately not collapsed: they guard different windows.

    SR3-6  Rollback guidance was driven by a variable, not by observation.
           The wrapper offered the rollback command only when the ledger's
           new_container_id had been populated -- a field written in section 6.
           A Compose run that partially replaced the container and then
           returned nonzero left that field null while the observer correctly
           reported the new container running, so the operator was denied the
           rollback in exactly the case that needed it. Fixed by
           Test-RollbackAdvisable, which reads the OBSERVER.

    SR3-7  -WhatIf is production-safe, not side-effect free.
           It fetches and prunes git refs and creates and removes a worktree.
           Documented as such rather than as "changes nothing", and -WhatIf
           with -Prs now says plainly that it validated the PR gates but never
           merged, so it has NOT qualified the tree a real deploy would build.

    WHAT A DEPLOY PROOF NEEDS, and which of these each part supplies:

        source identity     the tree built is exactly the target commit   OPS-1
        transport outcome   the build and activate commands succeeded     OPS-2
        artifact identity   the running container IS the image just built OPS-2
        recipe agreement    recovery would recreate the same thing        SR2-1
        storage identity    the binds resolve to the intended shares      SR3-1
        runtime outcome     the FINAL container behaves correctly         SR3-2
        state disclosure    on failure, what is actually running          OPS-5

    Invoke-DeployCore RETURNS a result object; it never calls exit. That is
    what lets tests/test_deploy_core_docker.ps1 drive it against a fixture and
    assert on the outcome. scripts/merge-and-deploy.ps1 is the production
    wrapper that supplies the real identities and translates the result into
    an exit code.
#>

# The storage-identity probes. Separate file because the rule they apply is
# LIFTED out of scripts/mount-nas-shares.ps1 rather than re-typed here -- see
# the long note at the top of that module for why a second copy of a safety
# rule is the defect and not the fix.
. (Join-Path $PSScriptRoot 'nas-probe.ps1')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

function New-DeployConfig {
    <#
      Defaults for everything the engine needs. Production values live in
      merge-and-deploy.ps1; fixtures override the identities and the timings.

      Nothing here is optional-with-a-silent-default in the dangerous
      direction: Repo, PinnedCompose, Container, Service and ImageTag have no
      usable default and are validated below.
    #>
    param([hashtable]$Override = @{})

    $c = @{
        # --- identities (no safe default) ---
        Repo              = $null   # --project-directory; relative bind mounts resolve here
        PinnedCompose     = $null   # the recovery recipe recreate would use
        Container         = $null
        Service           = $null
        ImageTag          = $null   # the RECOVERY identity. Promoted only after VERIFIED.
        CandidatePrefix   = $null   # e.g. 'scanhound:candidate-'
        MutexName         = $null   # shared with the recovery task
        # SR3-5. A SECOND lock, held for the WHOLE run, and deliberately NOT
        # the same one. MutexName above serialises this deploy against
        # ScanHound-MountNASShares and is taken late, because blocking mount
        # recovery for a ten-minute build would be worse than the race it
        # closes. That leaves deploy-vs-deploy unserialised, and two deploys of
        # the same commit do not collide occasionally -- they collide by
        # construction, because every derived name is a function of the SHA:
        #     worktree       scanhound-src-<sha12>
        #     candidate tag  <prefix>candidate-<sha12>
        #     override file  scanhound-candidate-<sha12>.yml
        # and New-CleanSource REMOVES a pre-existing worktree of that name
        # before creating it, so the second run deletes the first run's build
        # source out from under a running docker build.
        # Defaulted, NOT required. SR3-5's guarantee is "a real deploy always
        # holds a deploy lock" -- a default satisfies that, while making it
        # mandatory only pushed the burden onto callers that never deploy.
        # tests/test_nas_probe_pin.ps1 builds a config purely to reach
        # Resolve-NasRuntimeSpec; requiring this took that suite from
        # 14 passed / 0 failed to 12 passed / 2 failed, and one of the two was a
        # negative CONTROL that then refused for the WRONG REASON.
        # Still rejected when EMPTY below, so it can never mean "no lock".
        # Fixtures MUST override it -- the Docker suite's identity tripwire
        # asserts both mutex names belong to the fixture.
        DeployMutexName   = 'Global\ScanHound-Deploy'
        # Zero, not a wait. A second deploy that queued would sit behind a
        # ten-minute build and then deploy a ref that has moved since it was
        # asked for. Refusing immediately is the honest answer.
        DeployMutexTimeoutSec = 0

        # --- what to deploy ---
        Prs               = @()
        Ref               = 'origin/main'
        SkipMerge         = $false
        WhatIf            = $false

        # --- runtime expectations ---
        HealthUrl         = $null   # $null skips the health assertion
        PortHost          = $null   # $null skips the port assertion
        PortNum           = 0        # the HOST port that must be published
        # The CONTAINER port the publish maps to. Defaults to PortNum, which is
        # right for ScanHound (127.0.0.1:9721->9721) and was silently WRONG in
        # general: .NetworkSettings.Ports is keyed by CONTAINER port, so a
        # fixture publishing host 27048 -> container 8080 looked "NOT BOUND"
        # while /health answered fine. Production never exposed it because its
        # two numbers are equal.
        ContainerPort     = 0
        RequireEnvVar     = $null   # $null skips the env assertion
        SettleSeconds     = 15
        LogWindowSeconds  = 180
        SpamPattern       = $null   # $null skips the log window entirely
        SpamThreshold     = 12

        # --- SR3-1: storage identity ---
        # $false skips every storage proof, which is right for a deployment
        # that has no bind mounts to prove and wrong for ScanHound.
        NasProbe          = $false
        # Where the identity RULE is read from, relative to the clean source.
        # The target commit's copy, not the operator's working tree, for the
        # same reason the build context is the target commit's tree.
        NasMountScriptRel = 'scripts\mount-nas-shares.ps1'
        # $null means "derive the whole spec from that script" -- which is what
        # production wants, because then the deploy engine and the recovery
        # task cannot disagree about which shares exist. A fixture supplies its
        # own Mounts/CriticalTarget/FsType, because it cannot create a 9p NAS
        # share and is modelling the SHAPE of the failure, not 9p itself.
        NasMounts         = $null   # @( @{ HostPath; Target; Origin; ReadOnly } )
        NasCriticalTarget = $null
        NasFsType         = $null
        NasProbeTimeoutSec = 90
        # The image the throwaway host-source probe container runs. $null uses
        # the candidate image, which is local by construction, so the host
        # proof can never wait on a registry pull.
        NasHostProbeImage = $null

        # --- scaffolding ---
        WorkRoot          = $env:TEMP
        MutexTimeoutSec   = 300
        Quiet             = $false

        # --- test seams. Production leaves both null. ---
        # OnAfterActivate runs between activation and the identity check so a
        # fixture can create a genuinely wrong running image (case C) without
        # the test reimplementing the verifier it is trying to qualify.
        OnAfterActivate   = $null
        # OnAfterReconcile runs between the plain-Compose reconcile and the
        # FINAL runtime checks, so a fixture can break the final container
        # without reimplementing the checks it is trying to qualify (SR3-2).
        OnAfterReconcile  = $null
        SkipPrGate        = $false  # fixtures have no GitHub
    }
    foreach ($k in $Override.Keys) {
        if (-not $c.ContainsKey($k)) { throw "unknown deploy config key '$k'" }
        $c[$k] = $Override[$k]
    }
    foreach ($k in @('Repo','PinnedCompose','Container','Service','ImageTag','CandidatePrefix','MutexName','DeployMutexName')) {
        if (-not $c[$k]) { throw "deploy config is missing required key '$k'" }
    }
    if (-not $c['ContainerPort']) { $c['ContainerPort'] = $c['PortNum'] }
    return $c
}

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

$script:Q = $false
function Say ([string]$m) { if (-not $script:Q) { Write-Host "  $m" } }
function Head([string]$m) { if (-not $script:Q) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan } }
function Good([string]$m) { if (-not $script:Q) { Write-Host "  OK   $m" -ForegroundColor Green } }
function Warn([string]$m) { if (-not $script:Q) { Write-Host "  WARN $m" -ForegroundColor Yellow } }

# ---------------------------------------------------------------------------
# Native command execution
# ---------------------------------------------------------------------------

function Invoke-Native {
    <#
      Run a native exe; return BOTH its output and its exit code.

      OPS-3: the first version returned only strings, so callers decided
      success from the presence or absence of text. A `gh` auth failure
      produced error text containing no "fail" row, and the PR gate concluded
      "all checks passing".

      PowerShell 5.1 quirks handled here:
        * a native program's stderr is dropped unless redirected;
        * with 2>&1 each stderr line arrives as an ErrorRecord, not a string;
        * under $ErrorActionPreference='Stop' the first such record TERMINATES.

      None of that redefines the process exit code, so the exit code is what
      is kept and returned.
    #>
    param([Parameter(Mandatory)][scriptblock]$Command)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $global:LASTEXITCODE = 0
        $out  = & $Command 2>&1 | ForEach-Object { $_.ToString() }
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    [pscustomobject]@{ Output = @($out); ExitCode = $code; Text = (@($out) -join "`n") }
}

function Require-Native {
    <# Same, but a nonzero exit stops the deploy. No evidence is not passing evidence. #>
    param([Parameter(Mandatory)][scriptblock]$Command, [string]$What)
    $r = Invoke-Native $Command
    if ($r.ExitCode -ne 0) {
        $tail = (@($r.Output) | Select-Object -Last 12 | ForEach-Object { "      $_" }) -join "`n"
        Stop-Deploy ("{0} failed (exit {1}):`n{2}" -f $What, $r.ExitCode, $tail)
    }
    return $r
}

function Stop-Deploy {
    <#
      A refusal. Throws rather than exiting so the caller still gets the
      ledger and the finally-block observation -- exiting from inside a
      library would also kill any test runner hosting it.
    #>
    param([string]$Message)
    $script:DeployStopped = $true
    $script:D.stop_reason = $Message
    throw $Message
}

# ---------------------------------------------------------------------------
# Source identity (OPS-1)
# ---------------------------------------------------------------------------

function New-CleanSource {
    <#
      Materialise the target commit into a disposable worktree and build from
      THAT, instead of trusting the primary checkout.

      Why not "reject every ?? line" in git status: it is not sufficient.
      Git-ignored files never appear in ordinary `git status` at all, and
      Docker includes them unless .dockerignore also excludes them. Measured
      on this repo 2026-08-26: frontend/src-tauri/gen, 1.7 GB, ignored by
      .gitignore, absent from .dockerignore, inside COPY frontend/.

      A fresh worktree has exactly the tracked tree at that commit and nothing
      else, so the three problems close together: no untracked contamination,
      no ignored-local contamination, and the operator's branch is not moved.
    #>
    param([string]$Repo, [string]$Sha, [string]$WorkRoot)

    # SR3-5. This path is a pure function of the SHA and the next two lines
    # DELETE whatever is sitting at it. That is correct for the leftovers of a
    # killed run and catastrophic for a concurrent one, which is why the
    # deploy-instance mutex is held for the whole run before this point.
    $dir = Join-Path $WorkRoot ("scanhound-src-{0}" -f $Sha.Substring(0, 12))
    if (Test-Path -LiteralPath $dir) {
        Invoke-Native { git -C $Repo worktree remove --force $dir } | Out-Null
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Require-Native { git -C $Repo worktree add --detach $dir $Sha } "creating a clean worktree at $($Sha.Substring(0,12))" | Out-Null

    # Prove it, rather than trusting `worktree add`.
    $h = (Require-Native { git -C $dir rev-parse HEAD } "reading the clean worktree HEAD").Output[0].Trim()
    if ($h -ne $Sha) { Stop-Deploy "the clean worktree is at $h, not the target $Sha." }

    # And prove it is clean, INCLUDING untracked -- unlike the primary
    # checkout, here there is no reason for anything to be present.
    $st = Invoke-Native { git -C $dir status --porcelain }
    if ($st.ExitCode -ne 0) { Stop-Deploy "could not read the clean worktree status (exit $($st.ExitCode))." }
    if (@($st.Output).Count -gt 0) {
        @($st.Output) | Select-Object -First 10 | ForEach-Object { Say "    $_" }
        Stop-Deploy "the freshly created worktree is not clean. Refusing to build an unidentifiable source."
    }
    return $dir
}

function Remove-CleanSource {
    param([string]$Repo, [string]$Dir)
    if (-not $Dir) { return }
    Invoke-Native { git -C $Repo worktree remove --force $Dir } | Out-Null
    Remove-Item -LiteralPath $Dir -Recurse -Force -ErrorAction SilentlyContinue
    Invoke-Native { git -C $Repo worktree prune } | Out-Null
}

# ---------------------------------------------------------------------------
# Compose (SR2-1)
# ---------------------------------------------------------------------------

function Assert-ComposeAgrees {
    <#
      SR2-1. The pinned recovery recipe must render identically to the recipe
      being deployed. `config` renders what would ACTUALLY deploy, so comments
      and formatting do not register -- only semantic differences.

      The reference is the TARGET recipe, not whatever the operator happened
      to have checked out. Round 2 compared against the pre-deploy checkout
      before merging PRs, so a PR that edited docker-compose.yml deployed
      cleanly and left the pinned file stale.

      NOT ceremonial: on 2026-08-11/12 ScanHound-MountNASShares recreated the
      container from the pinned copy after it had gone stale. It lost
      SCANHOUND_DV_INGEST_KEY_SHA256 and the 127.0.0.1:9721 publish, and DV
      posts failed with WinError 10061 until someone noticed.
    #>
    param([string]$Pinned, [string]$TargetCompose, [string]$ProjectDir, [string]$When)

    $p = Require-Native { docker compose -f $Pinned        --project-directory $ProjectDir config } "rendering the pinned compose ($When)"
    $t = Require-Native { docker compose -f $TargetCompose --project-directory $ProjectDir config } "rendering the target compose ($When)"
    if (Compare-Object -ReferenceObject @($p.Output) -DifferenceObject @($t.Output)) {
        Stop-Deploy ("the pinned recovery compose does not match the recipe being deployed ($When). " +
                     "Deploying now would be reverted the next time the recovery task recreates " +
                     "the container. The pinned file is ACL'd to SYSTEM+Administrators, so " +
                     "reconciling it needs an elevated console.")
    }
    Good "pinned recovery recipe matches the target recipe ($When)"
}

function Assert-BuildIsPlain {
    <#
      The engine builds with a bare `docker build <context>` so it can choose
      the image tag, while recovery and any human use `docker compose`. Those
      two paths only agree while the compose build section is nothing more
      than the ROOT of the source tree plus the DEFAULT Dockerfile.

      SR3-4. The previous version permitted context, dockerfile and
      dockerfile_inline, and RETURNED the rendered context -- which the engine
      then ignored, because it always builds

          docker build -t <candidate> -f <clean-root>/Dockerfile <clean-root>

      So a reviewed compose change to `context: ./subdir` or
      `dockerfile: Dockerfile.production` would have been explicitly ACCEPTED
      by this guard while the engine built a different thing, and the ledger
      would have claimed the target commit's provenance for it. That is the
      OPS-1 defect wearing a different hat: a check that passes on semantics
      nobody honours.

      THE CHOICE MADE HERE IS TO REFUSE, NOT TO HONOUR, and the reason is that
      the rendered context is not a value this engine can safely honour.
      Compose resolves it against --project-directory, which is the OPERATOR'S
      checkout and not the clean worktree: for ScanHound, `build: .` renders as
      the literal string X:\Docker Apps\ScanHound. Honouring that value would
      build the dirty primary checkout on whatever branch the operator happens
      to be standing -- which is exactly the trap a hand deploy fell into on
      2026-08-26, where `docker compose up -d --build --project-directory
      <repo>` would have built the ops branch instead of main. Honouring it
      RELATIVELY, by re-rooting it into the clean worktree, is implementable,
      but it would add a second path-rewriting rule whose only user is a
      compose layout that does not exist and that no reviewer has seen.

      So the contract is now exactly what the engine does:
        * the rendered context must BE the project root, so that re-rooting it
          into the clean worktree is the identity operation;
        * a named dockerfile must be the default Dockerfile at that root;
        * dockerfile_inline is refused -- there is no file to hand to -f;
        * anything else (args, target, secrets, ssh) is refused as before.

      It returns NOTHING. Returning a context the caller ignores is what let
      the previous version read like a check.
    #>
    param([string]$TargetCompose, [string]$ProjectDir, [string]$Service)

    $j = Require-Native { docker compose -f $TargetCompose --project-directory $ProjectDir config --format json } "rendering the target compose as json"
    $cfg = $null
    try { $cfg = $j.Text | ConvertFrom-Json } catch { Stop-Deploy "the target compose did not render as JSON: $($j.Text)" }
    $svc = $cfg.services.$Service
    if (-not $svc) { Stop-Deploy "the target compose has no service '$Service'." }
    if ($svc.PSObject.Properties.Name -notcontains 'build') {
        Stop-Deploy "the target compose service '$Service' has no build section; this engine builds from source."
    }

    # dockerfile_inline is NOT in this list any more: the engine builds -f
    # <file>, and an inline definition is not a file.
    # SERVICE-LEVEL keys that change what a build produces, checked BEFORE the
    # build section. `platform:` is the one that matters and it does NOT live
    # under services.<svc>.build: `docker compose config --format json` renders
    # it as a sibling of build, so a guard that only inspects $svc.build passes
    # while `docker compose build --print` resolves the same file to
    # target.app.platforms = ["linux/arm64"]. This engine's `docker build`
    # passes no --platform, so compose and the engine would build DIFFERENT
    # images while the ledger reported the target commit's provenance for the
    # engine's -- a false provenance claim, which is OPS-1's defect wearing a
    # different key.
    $svcLevelBuildAffecting = @('platform')
    $svcExtra = @($svc.PSObject.Properties.Name | Where-Object { $svcLevelBuildAffecting -contains $_ })
    if ($svcExtra.Count -gt 0) {
        Stop-Deploy ("the target compose service '$Service' sets {0}, which changes what a build " +
                     "produces and which this engine's plain docker build does not pass. Teach the " +
                     "engine that option or build through compose." -f ($svcExtra -join ', '))
    }

    $allowed = @('context', 'dockerfile')
    $extra = @($svc.build.PSObject.Properties.Name | Where-Object { $allowed -notcontains $_ })
    if ($extra.Count -gt 0) {
        Stop-Deploy ("the compose build section uses {0}, which this engine's plain docker build would " +
                     "not reproduce. Teach the engine those options or build through compose." -f ($extra -join ', '))
    }

    $ctx = "$($svc.build.context)"
    if (-not $ctx) { Stop-Deploy "the compose build section renders no build context." }
    if (-not [System.IO.Path]::IsPathRooted($ctx)) {
        # Compose always renders an absolute context. If that ever changes,
        # resolving it here against this process's current directory would be
        # a guess, and a guess is not a proof.
        Stop-Deploy "the compose build context rendered as the relative path '$ctx'; this engine will not guess what it is relative to."
    }
    $ctxFull  = ([System.IO.Path]::GetFullPath($ctx)).TrimEnd('\', '/')
    $rootFull = ([System.IO.Path]::GetFullPath($ProjectDir)).TrimEnd('\', '/')
    if ($ctxFull -ne $rootFull) {
        Stop-Deploy ("the compose build context renders as '$ctxFull', not the project root '$rootFull'. " +
                     "This engine builds the ROOT of the clean worktree, so it would build a different " +
                     "tree than compose does and then report the target commit as its provenance. Move " +
                     "the build context back to the project root, or teach the engine to re-root it.")
    }

    $df = 'Dockerfile'
    if ($svc.build.PSObject.Properties.Name -contains 'dockerfile') { $df = "$($svc.build.dockerfile)" }
    # -cne, case sensitively: the daemon reads this path on a case-sensitive
    # filesystem even when the client does not.
    if ($df -cne 'Dockerfile') {
        Stop-Deploy ("the compose build section names dockerfile '$df'. This engine always builds the " +
                     "default Dockerfile at the root of the clean worktree, so it would build a different " +
                     "recipe than compose does. Rename the file, or teach the engine the override.")
    }
    Good "compose builds the project root with the default Dockerfile -- the same thing this engine builds"
}

# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

function Get-ImageId {
    <# The image id behind a tag, or $null when the tag does not exist.
       Absence is a legitimate answer here (first deploy), so it is reported
       as $null rather than treated as a failure. #>
    param([string]$Tag)
    $r = Invoke-Native { docker image inspect $Tag --format '{{.Id}}' }
    if ($r.ExitCode -ne 0 -or @($r.Output).Count -eq 0) { return $null }
    return $r.Output[0].Trim()
}

# ---------------------------------------------------------------------------
# OPS-5: observe, never act, never throw
# ---------------------------------------------------------------------------

function Observe-CurrentContainerState {
    <#
      OPS-5. After destructive work has been attempted the operator needs an
      answer to "what is running right now?", not a replay of variables that
      were populated before the error.

      Two hard rules:
        * this function can never throw -- it runs from a finally block, and
          an observer that throws destroys the report it exists to produce;
        * this function can never change state -- every command is an inspect,
          a read or an HTTP GET.

      Every field is either a value or the string UNKNOWN. Nothing here
      collapses "could not measure" into a clean-looking answer.
    #>
    param([hashtable]$Cfg)

    $o = [ordered]@{
        container_id   = 'UNKNOWN'
        image_id       = 'UNKNOWN'
        running        = 'UNKNOWN'
        started_at     = 'UNKNOWN'
        port           = 'UNKNOWN'
        health         = 'UNKNOWN'
        recovery_tag   = 'UNKNOWN'
    }
    try {
        $i = Invoke-Native { docker inspect -f '{{.Id}}|{{.Image}}|{{.State.Running}}|{{.State.StartedAt}}' $Cfg.Container }
        if ($i.ExitCode -eq 0 -and @($i.Output).Count -gt 0) {
            $p = $i.Output[0].Split('|')
            if ($p.Count -ge 4) {
                $o.container_id = $p[0].Substring(0, [Math]::Min(12, $p[0].Length))
                $o.image_id     = $p[1].Substring(0, [Math]::Min(19, $p[1].Length))
                $o.running      = $p[2]
                $o.started_at   = $p[3]
            }
        } else {
            $o.container_id = 'ABSENT'; $o.image_id = 'ABSENT'; $o.running = 'ABSENT'; $o.started_at = 'ABSENT'
        }
    } catch { }

    try {
        if ($Cfg.PortHost) {
            $pj = Invoke-Native { docker inspect -f '{{json .NetworkSettings.Ports}}' $Cfg.Container }
            if ($pj.ExitCode -eq 0) {
                $ports = $pj.Text | ConvertFrom-Json
                $key = "$($Cfg.ContainerPort)/tcp"
                $hit = $null
                if ($ports -and $ports.PSObject.Properties.Name -contains $key -and $ports.$key) {
                    foreach ($e in @($ports.$key)) {
                        if ($e.HostIp -eq $Cfg.PortHost -and [int]$e.HostPort -eq $Cfg.PortNum) { $hit = "$($e.HostIp):$($e.HostPort)" }
                    }
                }
                $o.port = if ($hit) { $hit } else { 'NOT BOUND' }
            }
        } else { $o.port = 'n/a' }
    } catch { }

    try {
        if ($Cfg.HealthUrl) {
            $h = Invoke-RestMethod -Uri $Cfg.HealthUrl -TimeoutSec 10
            $o.health = if ($h -and $h.PSObject.Properties.Name -contains 'status') { "status=$($h.status)" } else { 'answered, no status field' }
        } else { $o.health = 'n/a' }
    } catch { $o.health = 'NO ANSWER' }

    try {
        $t = Get-ImageId $Cfg.ImageTag
        $o.recovery_tag = if ($t) { $t.Substring(0, [Math]::Min(19, $t.Length)) } else { 'ABSENT' }
    } catch { }

    return $o
}

function Test-RollbackAdvisable {
    <#
      SR3-6. Should the operator be offered the one-command rollback?

      The wrapper used to ask `new_container_id -and -not promoted`.
      new_container_id is written in section 6, so a Compose run that PARTIALLY
      replaced the container and then returned nonzero left it null -- while
      Observe-CurrentContainerState, running from the finally, correctly
      reported a NEW container running. The operator was therefore denied the
      rollback command in precisely the case that needed it most: production
      already serving an unqualified candidate, with no ledger field admitting
      it.

      So the decision is driven by the OBSERVER, which measures state after the
      failure instead of replaying a variable written before it.

      Two conditions, and no third:
        * the recovery tag has NOT been promoted -- if it had, the pinned
          recovery recipe would recreate the SAME image and the command would
          not be a rollback at all;
        * the container running now is not the container that was running
          before, including the case where there was no container before.

      'UNKNOWN' and 'ABSENT' are not container ids. An observation that could
      not be made is not evidence that production was replaced.
    #>
    param($Ledger)
    if ($Ledger.promoted) { return $false }
    $observedId = "$($Ledger.observed.container_id)"
    if ($observedId -eq '' -or $observedId -eq 'UNKNOWN' -or $observedId -eq 'ABSENT') { return $false }
    return ($observedId -ne "$($Ledger.old_container_id)")
}

function Show-Ledger {
    param($Ledger)
    Write-Host ""
    Write-Host "== State ledger" -ForegroundColor Cyan
    foreach ($k in $Ledger.Keys) {
        $v = $Ledger[$k]
        if ($v -is [System.Collections.IDictionary]) {
            Write-Host ("  {0,-18}" -f $k)
            foreach ($k2 in $v.Keys) { Write-Host ("    {0,-16} {1}" -f $k2, $v[$k2]) }
            continue
        }
        if ($v -is [array]) { $v = ($v -join ', ') }
        if ($null -eq $v -or "$v" -eq '') { $v = '-' }
        Write-Host ("  {0,-18} {1}" -f $k, $v)
    }
}

# ---------------------------------------------------------------------------
# SR3-1: storage identity
# ---------------------------------------------------------------------------

function Resolve-NasRuntimeSpec {
    <#
      Build the storage spec this run will prove, from the TARGET commit's copy
      of the recovery script.

      Production passes no Mounts, so all nine shares, their sources, their
      expected origins and the critical read-write destination come out of
      mount-nas-shares.ps1 itself -- there is no second list to go stale.

      A fixture passes its own Mounts because it cannot create a 9p NAS share.
      It still gets the identity RULE from the real script; only the targets,
      origins, filesystem type and critical destination are its own.
    #>
    param([hashtable]$Cfg, [string]$SourceDir)

    # NOT $script: that name reads like the scope modifier used everywhere else
    # in this file and the confusion is not worth the two saved characters.
    $mountScript = Join-Path $SourceDir $Cfg.NasMountScriptRel
    if (-not (Test-Path -LiteralPath $mountScript -PathType Leaf)) {
        Stop-Deploy ("storage proofs are enabled but the target commit has no " +
                     "$($Cfg.NasMountScriptRel). The identity rule is read from there, " +
                     "and this engine will not invent one.")
    }

    try {
        $probe = Get-NasProbeScript -MountScriptPath $mountScript
    } catch {
        Stop-Deploy "the storage identity rule could not be lifted from $($Cfg.NasMountScriptRel): $($_.Exception.Message)"
    }

    if ($Cfg.NasMounts) {
        if (-not $Cfg.NasCriticalTarget) { Stop-Deploy "NasMounts was supplied without NasCriticalTarget." }
        if (-not $Cfg.NasFsType)         { Stop-Deploy "NasMounts was supplied without NasFsType." }
        $mounts   = @($Cfg.NasMounts)
        $critical = $Cfg.NasCriticalTarget
        $fsType   = $Cfg.NasFsType
    } else {
        try { $derived = Get-NasSpec -MountScriptPath $mountScript }
        catch { Stop-Deploy "the storage spec could not be derived from $($Cfg.NasMountScriptRel): $($_.Exception.Message)" }
        $mounts   = @($derived.Mounts)
        $critical = $derived.CriticalTarget
        $fsType   = $derived.FsType
    }

    if (@($mounts).Count -eq 0) { Stop-Deploy "the storage spec names no mounts; an empty proof is not a proof." }
    if (@($mounts | Where-Object { $_.Target -eq $critical }).Count -ne 1) {
        Stop-Deploy "the critical target '$critical' appears $(@($mounts | Where-Object { $_.Target -eq $critical }).Count) time(s) in the storage spec; expected exactly one."
    }

    return @{
        ProbeScript    = $probe
        Mounts         = $mounts
        CriticalTarget = $critical
        FsType         = $fsType
        Targets        = @($mounts | ForEach-Object { $_.Target })
        DataText       = (ConvertTo-NasProbeData -Mounts $mounts)
    }
}

function Test-NasInContainer {
    <#
      Run the storage proof inside a container and turn its result into
      problems/unknowns. Kept here rather than in the probe module because the
      POLICY -- what counts as a refusal -- belongs to the deploy, while the
      identity rule belongs to the recovery script.

      A probe that could not run is UNKNOWN, never a pass. That distinction is
      the whole reason the 2026-07-26 outage was invisible.
    #>
    param([hashtable]$Cfg, $Spec, [string]$Container, [string]$Phase)

    $r = Invoke-NasProbeInContainer -Container $Container -ScriptText $Spec.ProbeScript `
             -DataText $Spec.DataText -CriticalTarget $Spec.CriticalTarget -FsType $Spec.FsType `
             -Targets $Spec.Targets -TimeoutSec $Cfg.NasProbeTimeoutSec -WorkRoot $Cfg.WorkRoot

    $problems = @(); $unknown = @()
    if ($r.Reason -ne 'probed') {
        $unknown += "the $Phase container's bind mounts could not be probed ($($r.Reason)) -- storage identity UNKNOWN, not verified"
    } elseif ($r.Code -ne 0) {
        $detail = (@($r.Output -split "`n") | Where-Object { $_ -notmatch '^OK\s' } | Select-Object -First 6) -join '; '
        $problems += ("the $Phase container's bind mounts are NOT the intended shares (probe exit $($r.Code)): $detail")
    } else {
        Good "$Phase container: all $(@($Spec.Targets).Count) bind mounts identity-verified, $($Spec.CriticalTarget) writable and deletable"
    }
    return @{ Problems = $problems; Unknown = $unknown; Reason = $r.Reason; Code = $r.Code; Output = $r.Output }
}

# ---------------------------------------------------------------------------
# SR3-2: the cheap checks, as one function called twice
# ---------------------------------------------------------------------------

function Invoke-RuntimeChecks {
    <#
      Everything that can be asserted about a container in a few seconds:
      it is running, the exact publish exists, the required env is set, /health
      says ok, and its bind mounts resolve to the intended shares.

      SR3-2. This exists as a function because it is called TWICE -- once
      against the candidate the deploy activated, and again against whatever
      the post-promotion reconcile leaves running. Round 3 checked the second
      container for image id only and then declared VERIFIED, so a reconcile
      that recreated the container into a broken instance-level state passed.

      The container is looked up BY NAME every time, and the id it actually
      observed is returned, so the caller can prove which container was
      qualified rather than assuming it was the same one.

      Returns problems and unknowns; it never throws for a check result and
      never decides the verdict.
    #>
    param([hashtable]$Cfg, $NasSpec, [Parameter(Mandatory)][string]$Phase)

    $problems = @(); $unknown = @(); $cid = $null

    $idr = Invoke-Native { docker inspect -f '{{.Id}}' $Cfg.Container }
    if ($idr.ExitCode -ne 0 -or @($idr.Output).Count -eq 0) {
        $unknown += "could not read the $Phase container's id"
    } else {
        $cid = $idr.Output[0].Trim().Substring(0, 12)
        Say "$Phase checks against container $cid"
    }

    $run = Invoke-Native { docker inspect -f '{{.State.Running}}' $Cfg.Container }
    if ($run.ExitCode -ne 0)            { $unknown  += "could not read the $Phase running state" }
    elseif ($run.Output[0] -ne 'true')  { $problems += "$Phase container is not running" }
    else                                { Good "${Phase}: running" }

    if ($Cfg.PortHost) {
        # Exact binding, not a substring: '127.0.0.1:97210' contains '127.0.0.1:9721'.
        $pj = Invoke-Native { docker inspect -f '{{json .NetworkSettings.Ports}}' $Cfg.Container }
        if ($pj.ExitCode -ne 0) { $unknown += "could not read the $Phase port bindings" }
        else {
            $ports = $pj.Text | ConvertFrom-Json
            $key = "$($Cfg.ContainerPort)/tcp"
            $bound = $false
            if ($ports -and $ports.PSObject.Properties.Name -contains $key -and $ports.$key) {
                foreach ($e in @($ports.$key)) {
                    if ($e.HostIp -eq $Cfg.PortHost -and [int]$e.HostPort -eq $Cfg.PortNum) { $bound = $true }
                }
            }
            if ($bound) { Good "${Phase}: $($Cfg.PortHost):$($Cfg.PortNum) bound exactly" }
            else        { $problems += "${Phase}: $($Cfg.PortHost):$($Cfg.PortNum) is NOT bound" }
        }
    }

    if ($Cfg.RequireEnvVar) {
        $ev = $Cfg.RequireEnvVar
        $k = Invoke-Native { docker exec $Cfg.Container sh -c "test -n `"`$$ev`" && echo SET || echo MISSING" }
        if ($k.ExitCode -ne 0)        { $unknown  += "could not read the $ev env in the $Phase container" }
        elseif ($k.Text -match 'SET') { Good "${Phase}: $ev set" }
        else                          { $problems += "${Phase}: $ev MISSING" }
    }

    if ($Cfg.HealthUrl) {
        try {
            $h = Invoke-RestMethod -Uri $Cfg.HealthUrl -TimeoutSec 20
            if ($h.status -eq 'ok') { Good "${Phase}: /health status=ok" }
            else { $problems += "${Phase}: /health answered but status=$($h.status), not ok" }
        } catch {
            $problems += "${Phase}: /health did not answer: $($_.Exception.Message)"
        }
    }

    $nas = $null
    if ($NasSpec) {
        $nas = Test-NasInContainer -Cfg $Cfg -Spec $NasSpec -Container $Cfg.Container -Phase $Phase
        $problems += $nas.Problems
        $unknown  += $nas.Unknown
    }

    return @{
        Problems    = @($problems)
        Unknown     = @($unknown)
        ContainerId = $cid
        NasReason   = $(if ($nas) { $nas.Reason } else { 'n/a' })
        NasCode     = $(if ($nas) { $nas.Code }   else { 'n/a' })
    }
}

# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

function Invoke-DeployCore {
    <#
      Returns a result object. Never exits. Never throws for an ordinary
      refusal -- a refusal comes back as verdict STOPPED with a stop_reason.
    #>
    param([Parameter(Mandatory)][hashtable]$Config)

    $cfg = $Config
    $script:Q = [bool]$cfg.Quiet
    $script:DeployStopped = $false

    $script:D = [ordered]@{
        target_sha       = $null
        merged_prs       = @()
        source_dir       = $null
        build_attempted  = $false
        build_exit       = $null
        candidate_tag    = $null
        built_image_id   = $null
        recovery_tag_before = $null
        recovery_tag_after  = $null
        promoted         = $false
        old_container_id = $null
        old_image_id     = $null
        new_container_id = $null
        new_image_id     = $null
        activate_exit    = $null
        # SR3-1
        nas_host_reason      = $null
        nas_host_code        = $null
        nas_candidate_reason = $null
        nas_candidate_code   = $null
        nas_final_reason     = $null
        nas_final_code       = $null
        # SR3-2: which container each round of cheap checks actually observed
        candidate_container_id    = $null
        reconcile_recreated       = $null
        final_checks_container_id = $null
        observed         = $null
        stop_reason      = $null
        problems         = @()
        unknown          = @()
        verdict          = 'not reached'
    }

    $src      = $null
    $override = $null
    $mutex    = $null
    $haveLock = $false
    $nasSpec  = $null
    # SR3-5. The whole-run deploy-instance lock, separate from the recovery
    # lock above and held for a deliberately different span.
    $deployMutex    = $null
    $haveDeployLock = $false

    try {
        # =================================================================
        Head "1. Pre-flight"
        # =================================================================
        # SR3-5. Taken FIRST and held until the finally block. Not the recovery
        # mutex: that one is taken much later, around the container transition
        # only, so a ten-minute build cannot block mount recovery. This one has
        # the opposite job -- everything this engine derives from the target
        # SHA is deterministic, so a second concurrent deploy would delete this
        # run's worktree, overwrite its candidate tag and rewrite its override
        # file. The two are not collapsed because their windows are different.
        $deployMutex = New-Object System.Threading.Mutex($false, $cfg.DeployMutexName)
        try   { $haveDeployLock = $deployMutex.WaitOne([TimeSpan]::FromSeconds($cfg.DeployMutexTimeoutSec)) }
        catch [System.Threading.AbandonedMutexException] { $haveDeployLock = $true }
        if (-not $haveDeployLock) {
            Stop-Deploy ("another deploy is already running: this run could not take the deploy-instance " +
                         "lock $($cfg.DeployMutexName). Two deploys of the same commit share a worktree " +
                         "path, a candidate tag and an override file, so the second one would delete the " +
                         "first one's build source mid-build. Nothing was changed.")
        }
        Good "holding the deploy-instance lock $($cfg.DeployMutexName) -- no second deploy can start"

        $tools = if ($cfg.SkipPrGate) { @('docker','git') } else { @('gh','docker','git') }
        foreach ($t in $tools) {
            if (-not (Get-Command $t -ErrorAction SilentlyContinue)) { Stop-Deploy "$t is not on PATH." }
        }
        Good ("{0} are available" -f ($tools -join ', '))
        if (-not (Test-Path -LiteralPath $cfg.PinnedCompose)) {
            Stop-Deploy "the pinned recovery compose is missing at $($cfg.PinnedCompose). Recovery would have nothing to recreate from."
        }

        # =================================================================
        Head "2. Pull requests"
        # =================================================================
        # OPS-3 (closed in round 2, unchanged here): structured state, not
        # text scraping. Every required check must be EXPLICITLY acceptable;
        # pending, cancelled and unknown are refusals.
        if ($cfg.SkipPrGate -or $cfg.SkipMerge -or @($cfg.Prs).Count -eq 0) {
            Warn "not merging anything"
        } else {
            foreach ($pr in $cfg.Prs) {
                Head ("2.{0}  PR #{0}" -f $pr)
                $v = Require-Native { gh pr view $pr --json state,mergeable,mergeStateStatus } "gh pr view #$pr"
                $info = $v.Text | ConvertFrom-Json
                if ($info.state -eq 'MERGED') { Good "already merged"; continue }
                if ($info.state -ne 'OPEN')   { Stop-Deploy "PR #$pr is $($info.state), not OPEN." }
                if ($info.mergeable -ne 'MERGEABLE') { Stop-Deploy "PR #$pr mergeable=$($info.mergeable)." }

                $c = Invoke-Native { gh pr checks $pr --json name,state,bucket }
                # gh documents exit 8 for "checks pending". Any other nonzero
                # is a measurement failure and must not read as success.
                if ($c.ExitCode -ne 0 -and $c.ExitCode -ne 8) {
                    Stop-Deploy "could not read checks for #$pr (gh exit $($c.ExitCode)): $($c.Text)"
                }
                $rows = @()
                try { $rows = @($c.Text | ConvertFrom-Json) } catch { Stop-Deploy "checks for #$pr did not parse as JSON: $($c.Text)" }
                if ($rows.Count -eq 0) { Stop-Deploy "PR #$pr reported NO checks. Absence is not success." }
                $bad = @($rows | Where-Object { $_.bucket -ne 'pass' -and $_.bucket -ne 'skipping' })
                if ($bad.Count -gt 0) {
                    foreach ($b in $bad) { Say ("  {0}: {1}" -f $b.name, $b.bucket) }
                    Stop-Deploy ("PR #$pr has {0} check(s) not passing. Nothing is whitelisted: a pending check is unknown, and unknown is not passing." -f $bad.Count)
                }
                Good ("all {0} check(s) passing" -f $rows.Count)
                if ($cfg.WhatIf) { Warn "-WhatIf: would merge #$pr"; continue }
                Require-Native { gh pr merge $pr --merge } "merging #$pr" | Out-Null
                $script:D.merged_prs += $pr
                Good "merged"
                Start-Sleep -Seconds 10
            }
        }

        # =================================================================
        Head "3. Source identity"
        # =================================================================
        Require-Native { git -C $cfg.Repo fetch origin --prune } "git fetch" | Out-Null
        Good "fetched origin"

        $target = (Require-Native { git -C $cfg.Repo rev-parse $cfg.Ref } "resolving $($cfg.Ref)").Output[0].Trim()
        $script:D.target_sha = $target
        Say "target $($cfg.Ref) = $($target.Substring(0,12))"

        # OPS-1 dry-run defect: round 2 skipped the checkout under -WhatIf and
        # then reported HEAD, so a dry run announced the branch the operator
        # happened to be on rather than the ref it would deploy. The target is
        # now resolved the same way in both modes, because resolving does not
        # touch the working tree.
        foreach ($pr in $script:D.merged_prs) {
            $sha = (Invoke-Native { gh pr view $pr --json mergeCommit -q '.mergeCommit.oid' }).Output[0]
            if ($sha) {
                $anc = Invoke-Native { git -C $cfg.Repo merge-base --is-ancestor $sha $target }
                if ($anc.ExitCode -ne 0) { Stop-Deploy "PR #$pr merged as $sha but that is NOT an ancestor of the ref being deployed." }
                Good "PR #$pr's merge commit is in the deployed source"
            }
        }

        $src = New-CleanSource -Repo $cfg.Repo -Sha $target -WorkRoot $cfg.WorkRoot
        $script:D.source_dir = $src
        Good "clean source materialised at $src"

        $targetCompose = Join-Path $src 'docker-compose.yml'
        if (-not (Test-Path -LiteralPath $targetCompose)) { Stop-Deploy "the target commit has no docker-compose.yml." }

        # SR3-1. Resolved HERE, before the build and before -WhatIf returns, so
        # a rule that can no longer be lifted out of the recovery script is a
        # refusal on a dry run rather than a surprise ten minutes into a real
        # deploy.
        if ($cfg.NasProbe) {
            $nasSpec = Resolve-NasRuntimeSpec -Cfg $cfg -SourceDir $src
            Good ("storage identity rule lifted from $($cfg.NasMountScriptRel): {0} mount(s), fs {1}, critical {2}" -f `
                  @($nasSpec.Mounts).Count, $nasSpec.FsType, $nasSpec.CriticalTarget)
        } else {
            Warn "storage identity proofs are DISABLED for this deployment (NasProbe = false)"
        }

        # SR2-1, check one: against the actual target, as soon as it exists
        # and before a ten-minute build.
        Assert-ComposeAgrees -Pinned $cfg.PinnedCompose -TargetCompose $targetCompose `
                             -ProjectDir $cfg.Repo -When 'after target resolution'
        # No | Out-Null: SR3-4 removed the return value. A guard that hands
        # back a context nobody uses is how this defect stayed invisible.
        Assert-BuildIsPlain -TargetCompose $targetCompose -ProjectDir $cfg.Repo -Service $cfg.Service

        if ($cfg.WhatIf) {
            Warn "-WhatIf: would build and deploy $($target.Substring(0,12))"
            # SR3-7. Stated as the contract it actually has. -WhatIf is
            # production-safe; it is not literally side-effect free, and saying
            # "changes nothing" would be a claim this code does not support.
            Warn "-WhatIf DID fetch and prune git refs and create a temporary worktree (removed below). It did NOT merge a PR, build or tag an image, recreate a container, or mutate production."
            if (@($cfg.Prs).Count -gt 0 -and -not $cfg.SkipMerge) {
                Warn "-WhatIf checked the PR gates but never merged, so the post-merge tree does not exist. This run has NOT qualified the source a real deploy would build."
            }
            $script:D.verdict = 'plan only'
            return (New-Result $cfg)
        }

        # =================================================================
        Head "4. Build under a candidate tag"
        # =================================================================
        # OPS-2. The build must not write the recovery identity. Compose
        # declares `image: <ImageTag>`, so `docker compose build` would retag
        # it to the unverified candidate before activation was even attempted,
        # and the scheduled recovery task's `up --no-build --pull never` would
        # then be able to activate it.
        $script:D.recovery_tag_before = Get-ImageId $cfg.ImageTag

        $old = Invoke-Native { docker inspect -f '{{.Id}} {{.Image}}' $cfg.Container }
        if ($old.ExitCode -eq 0 -and @($old.Output).Count -gt 0) {
            $parts = $old.Output[0].Split(' ')
            $script:D.old_container_id = $parts[0].Substring(0, 12)
            $script:D.old_image_id     = $parts[1].Substring(0, 19)
            Say "old container $($script:D.old_container_id) on image $($script:D.old_image_id)"
        } else {
            Say "no existing container (first deploy)"
        }

        $candidate = "{0}{1}" -f $cfg.CandidatePrefix, $target.Substring(0, 12)
        $script:D.candidate_tag  = $candidate
        $script:D.build_attempted = $true
        Say "building $candidate from the clean source -- this can exceed 10 minutes"

        $b = Invoke-Native { docker build -t $candidate -f (Join-Path $src 'Dockerfile') $src }
        $script:D.build_exit = $b.ExitCode
        if ($b.ExitCode -ne 0) {
            @($b.Output) | Select-Object -Last 15 | ForEach-Object { Say "    $_" }
            Stop-Deploy "the BUILD failed (exit $($b.ExitCode)). The old container is untouched, and $($cfg.ImageTag) still points at the last known-good image."
        }
        Good "build succeeded (exit 0)"

        $built = Get-ImageId $candidate
        if (-not $built) { Stop-Deploy "the build reported success but $candidate does not exist." }
        $script:D.built_image_id = $built.Substring(0, 19)
        Good "built image $($script:D.built_image_id)"

        # The OPS-2 assertion itself. If this ever fails, the build promoted
        # the recovery identity and an unverified artifact is reachable by the
        # recovery task RIGHT NOW.
        $tagNow = Get-ImageId $cfg.ImageTag
        if ($tagNow -ne $script:D.recovery_tag_before) {
            Stop-Deploy ("the build moved $($cfg.ImageTag) from $($script:D.recovery_tag_before) to $tagNow. " +
                         "An unverified image is now in the recovery namespace and the scheduled recreate could activate it.")
        }
        Good "$($cfg.ImageTag) is untouched by the build -- the candidate is quarantined"

        # =================================================================
        Head "5. Activate the candidate"
        # =================================================================
        # SR2-1, check two: immediately before the destructive step.
        Assert-ComposeAgrees -Pinned $cfg.PinnedCompose -TargetCompose $targetCompose `
                             -ProjectDir $cfg.Repo -When 'immediately before activation'

        # The same named mutex the recovery task uses, so recovery cannot
        # recreate the container in the window between activation and the
        # promotion decision.
        $mutex = New-Object System.Threading.Mutex($false, $cfg.MutexName)
        try   { $haveLock = $mutex.WaitOne([TimeSpan]::FromSeconds($cfg.MutexTimeoutSec)) }
        catch [System.Threading.AbandonedMutexException] { $haveLock = $true }
        if (-not $haveLock) {
            Stop-Deploy ("could not acquire the RECOVERY lock $($cfg.MutexName) within " +
                         "$($cfg.MutexTimeoutSec)s. The recovery task is holding it; nothing was changed. " +
                         "This is NOT the whole-run lock, which was taken in section 1 (SR3-5).")
        }
        Good "holding the RECOVERY lock $($cfg.MutexName) -- the recovery task cannot recreate during activation"

        # =================================================================
        # SR3-1, host side: prove the SOURCES before anything is activated
        # against them, while holding the lock.
        # =================================================================
        # This matters most precisely because of the lock. Holding it stops
        # ScanHound-MountNASShares from recreating the container mid-deploy,
        # which also stops the only actor that normally repairs a missing NAS
        # mount. A deploy that recreates the container while /mnt/nas is empty
        # binds /library/tv -- the TV rename DESTINATION -- to an ordinary
        # directory inside the VM, and every other check in this file passes.
        #
        # A throwaway container binds exactly the source->target set the real
        # service uses and the lifted probe runs inside it, so what is measured
        # is what Docker will actually resolve at container-create time.
        if ($nasSpec) {
            $img = $(if ($cfg.NasHostProbeImage) { $cfg.NasHostProbeImage } else { $candidate })
            Say "proving the host storage sources through a throwaway container on $img"
            $hp = Invoke-NasHostSourceProbe -Mounts $nasSpec.Mounts -Image $img `
                      -ScriptText $nasSpec.ProbeScript -CriticalTarget $nasSpec.CriticalTarget `
                      -FsType $nasSpec.FsType -TimeoutSec $cfg.NasProbeTimeoutSec -WorkRoot $cfg.WorkRoot
            $script:D.nas_host_reason = $hp.Reason
            $script:D.nas_host_code   = $hp.Code
            if ($hp.Reason -ne 'probed') {
                Stop-Deploy ("the host storage sources could not be probed ($($hp.Reason)). UNKNOWN is not " +
                             "proven, and nothing has been activated against them. $($hp.Output)")
            }
            if ($hp.Code -ne 0) {
                @($hp.Output -split "`n") | Where-Object { $_ -notmatch '^OK\s' } | Select-Object -First 10 | ForEach-Object { Say "    $_" }
                Stop-Deploy ("the host storage sources are NOT the intended shares (probe exit $($hp.Code)). " +
                             "Recreating the container now would bind $($nasSpec.CriticalTarget) to whatever " +
                             "those paths currently are. Nothing was changed; run " +
                             "scripts\mount-nas-shares.ps1 and deploy again.")
            }
            Good "host storage sources identity-verified, including the critical read-write source"
        }

        # Compose activates the CANDIDATE via an override, so the recovery
        # identity is still the last known-good image throughout.
        $override = Join-Path $cfg.WorkRoot ("scanhound-candidate-{0}.yml" -f $target.Substring(0, 12))
        @(
            "services:"
            "  $($cfg.Service):"
            "    image: $candidate"
        ) -join "`n" | Set-Content -LiteralPath $override -Encoding ASCII

        $a = Invoke-Native { docker compose -f $targetCompose -f $override --project-directory $cfg.Repo up -d --no-build --force-recreate $cfg.Service }
        $script:D.activate_exit = $a.ExitCode
        if ($a.ExitCode -ne 0) {
            @($a.Output) | Select-Object -Last 15 | ForEach-Object { Say "    $_" }
            Stop-Deploy "ACTIVATION failed (exit $($a.ExitCode)) after a successful build. $($cfg.ImageTag) was NOT promoted; the recovery task would restore the previous image."
        }
        Good "activated (exit 0)"

        if ($cfg.OnAfterActivate) { & $cfg.OnAfterActivate $cfg }

        # =================================================================
        Head "6. Artifact identity"
        # =================================================================
        Start-Sleep -Seconds $cfg.SettleSeconds
        $now = Require-Native { docker inspect -f '{{.Id}} {{.Image}}' $cfg.Container } "inspecting the container"
        $np = $now.Output[0].Split(' ')
        $script:D.new_container_id = $np[0].Substring(0, 12)
        $script:D.new_image_id     = $np[1].Substring(0, 19)

        if ($script:D.old_container_id -and $script:D.new_container_id -eq $script:D.old_container_id) {
            Stop-Deploy "the container was NOT replaced (still $($script:D.new_container_id)). Nothing was deployed."
        }
        Good "container replaced: $($script:D.old_container_id) -> $($script:D.new_container_id)"

        if ($script:D.new_image_id -ne $script:D.built_image_id) {
            Stop-Deploy ("the running container is on image $($script:D.new_image_id) but the build produced $($script:D.built_image_id). It is running something else.")
        }
        Good "running the image just built"

        # =================================================================
        Head "7. Runtime -- the candidate"
        # =================================================================
        # SR3-2. These are the CHEAP checks, and they are a function precisely
        # because they run again after the reconcile. Everything expensive --
        # the log window below -- stays here and is not repeated.
        $c1 = Invoke-RuntimeChecks -Cfg $cfg -NasSpec $nasSpec -Phase 'candidate'
        $problems = @($c1.Problems)
        $unknown  = @($c1.Unknown)
        $script:D.candidate_container_id = $c1.ContainerId
        $script:D.nas_candidate_reason   = $c1.NasReason
        $script:D.nas_candidate_code     = $c1.NasCode

        if ($problems.Count -eq 0 -and $unknown.Count -eq 0 -and $cfg.SpamPattern) {
            # Honestly worded: this observes a window, it does not prove the
            # suppression mechanism. A window with no stuck batch reads zero
            # whether or not the fix works -- which is why the causal property
            # belongs in the unit fixture that deliberately creates one.
            Say "observing the log for $($cfg.LogWindowSeconds)s"
            Start-Sleep -Seconds $cfg.LogWindowSeconds
            $since = "$($cfg.LogWindowSeconds)s"
            $lg = Invoke-Native { docker logs $cfg.Container --since $since }
            if ($lg.ExitCode -ne 0) {
                # The original 0%-baseline defect in its post-deploy form:
                # a measurement failure must not become a clean result.
                $unknown += "could not read logs -- spam rate UNKNOWN, not zero"
            } else {
                $spam = @(@($lg.Output) | Select-String -SimpleMatch $cfg.SpamPattern).Count
                Say ("{0} lines in {1}s, {2} matching" -f @($lg.Output).Count, $cfg.LogWindowSeconds, $spam)
                if ($spam -gt $cfg.SpamThreshold) { $problems += "$spam '$($cfg.SpamPattern)' lines in $($cfg.LogWindowSeconds)s" }
                else { Good "no flood observed in this window (not a proof of the mechanism)" }
            }
        }

        $script:D.problems = $problems
        $script:D.unknown  = $unknown

        if ($problems.Count -gt 0 -or $unknown.Count -gt 0) {
            foreach ($u in $unknown)  { Write-Host "  UNKNOWN  $u" -ForegroundColor Yellow }
            foreach ($p in $problems) { Write-Host "  PROBLEM  $p" -ForegroundColor Red }
            $script:D.verdict = if ($problems.Count) { 'PROBLEMS' } else { 'UNKNOWN' }
            Warn "$($cfg.ImageTag) NOT promoted -- it still points at the last verified image."
            return (New-Result $cfg)
        }

        # =================================================================
        Head "8. Promote the image"
        # =================================================================
        # Only now does the verified artifact enter the recovery namespace.
        Require-Native { docker tag $candidate $cfg.ImageTag } "promoting $candidate to $($cfg.ImageTag)" | Out-Null
        $script:D.promoted = $true
        $script:D.recovery_tag_after = (Get-ImageId $cfg.ImageTag)
        if ($script:D.recovery_tag_after -ne $built) {
            Stop-Deploy "promotion did not take: $($cfg.ImageTag) is $($script:D.recovery_tag_after), not $built."
        }
        Good "$($cfg.ImageTag) now points at the verified image"

        # =================================================================
        Head "9. Final activation -- reconcile onto the plain recipe"
        # =================================================================
        # SR3-2. This is not cleanup after qualification; it is the LAST
        # activation, and whatever it leaves running is what production gets.
        #
        # The override changed the service's image NAME, so the container
        # carries a compose config hash the pinned recipe does not reproduce;
        # left alone, the deployed container and the recovery recipe would
        # disagree about their own identity even though the image content is
        # identical. No --force-recreate: if compose considers it already
        # converged, nothing happens and the container id below is unchanged.
        $rc = Invoke-Native { docker compose -f $targetCompose --project-directory $cfg.Repo up -d --no-build $cfg.Service }
        if ($rc.ExitCode -ne 0) {
            @($rc.Output) | Select-Object -Last 10 | ForEach-Object { Say "    $_" }
            $script:D.problems += "reconciling onto the plain recipe failed (exit $($rc.ExitCode)); the container config does not match the pinned recovery recipe"
            $script:D.verdict = 'PROBLEMS'
            return (New-Result $cfg)
        }
        $post = Require-Native { docker inspect -f '{{.Id}} {{.Image}}' $cfg.Container } "inspecting after reconcile"
        $pp = $post.Output[0].Split(' ')
        $script:D.reconcile_recreated = ($pp[0].Substring(0,12) -ne $script:D.new_container_id)
        if ($script:D.reconcile_recreated) {
            Say "reconcile recreated the container: $($script:D.new_container_id) -> $($pp[0].Substring(0,12))"
            $script:D.new_container_id = $pp[0].Substring(0, 12)
        }
        if ($pp[1].Substring(0,19) -ne $script:D.built_image_id) {
            Stop-Deploy "after reconciling, the container is on $($pp[1].Substring(0,19)), not the verified $($script:D.built_image_id)."
        }
        Good "container reconciled onto the pinned recipe, still on the verified image"

        if ($cfg.OnAfterReconcile) { & $cfg.OnAfterReconcile $cfg }

        # =================================================================
        Head "10. Runtime -- the container that will actually serve"
        # =================================================================
        # SR3-2, the finding itself: round 3 stopped at the image-id check
        # above and declared VERIFIED. The right image is not the same claim as
        # a working instance. A recreated container can have lost its publish,
        # its environment, its health or -- the SR3-1 case -- its bind mounts,
        # because bind sources are resolved at container-CREATE time.
        #
        # The three-minute log window is deliberately NOT repeated: it is an
        # observation of volume over time, it already ran against this image,
        # and re-running it would triple every deploy to re-observe a window
        # that was never a proof of the mechanism anyway.
        Start-Sleep -Seconds $cfg.SettleSeconds
        $c2 = Invoke-RuntimeChecks -Cfg $cfg -NasSpec $nasSpec -Phase 'final'
        $script:D.final_checks_container_id = $c2.ContainerId
        $script:D.nas_final_reason          = $c2.NasReason
        $script:D.nas_final_code            = $c2.NasCode
        $script:D.problems = @($script:D.problems) + @($c2.Problems)
        $script:D.unknown  = @($script:D.unknown)  + @($c2.Unknown)

        if (@($c2.Problems).Count -gt 0 -or @($c2.Unknown).Count -gt 0) {
            foreach ($u in $c2.Unknown)  { Write-Host "  UNKNOWN  $u" -ForegroundColor Yellow }
            foreach ($p in $c2.Problems) { Write-Host "  PROBLEM  $p" -ForegroundColor Red }
            $script:D.verdict = if (@($c2.Problems).Count) { 'PROBLEMS' } else { 'UNKNOWN' }
            # Said plainly rather than left for the operator to infer: the tag
            # WAS promoted, because the image passed its full qualification
            # against the candidate container. What failed is this final
            # instance. Demoting the tag is deliberately not done -- it would
            # leave the recovery task ready to recreate an OLDER image than the
            # one now running, which is a worse state than the one being
            # reported.
            Warn "$($cfg.ImageTag) WAS promoted (the image qualified); the FINAL container did not."
            return (New-Result $cfg)
        }

        $script:D.verdict = 'VERIFIED'
        Good ("deploy verified: correct source, quarantined build, correct artifact, " +
              "proven storage identity, healthy final container $($c2.ContainerId)")
        return (New-Result $cfg)
    }
    catch {
        if ($script:DeployStopped) {
            Write-Host ""
            Write-Host "  STOP $($script:D.stop_reason)" -ForegroundColor Red
            $script:D.verdict = 'STOPPED'
        } else {
            Write-Host ""
            Write-Host "  UNHANDLED: $($_.Exception.Message)" -ForegroundColor Red
            $script:D.stop_reason = "unhandled: $($_.Exception.Message)"
            $script:D.verdict = 'ABORTED (exception)'
        }
        return (New-Result $cfg)
    }
    finally {
        # OPS-5. Observation happens once destructive work has been ATTEMPTED,
        # which is the only case where the operator cannot infer the state.
        if ($script:D.build_attempted) {
            try { $script:D.observed = Observe-CurrentContainerState -Cfg $cfg } catch { }
        }
        # Printed HERE, after the observation, so the report the operator reads
        # includes what production actually looks like now (OPS-5).
        if (-not $script:Q) { try { Show-Ledger $script:D } catch { } }
        if ($haveLock -and $mutex) { try { $mutex.ReleaseMutex() } catch { } }
        if ($mutex) { try { $mutex.Dispose() } catch { } }
        # SR3-5, and the ORDER HERE WAS WRONG. Both cleanups below touch paths
        # derived from the target SHA -- the override file
        # <prefix>candidate-<sha12>.yml and the worktree scanhound-src-<sha12>.
        # Releasing the deploy lock before them left a window in which a second
        # deploy OF THE SAME COMMIT -- the immediate-retry-after-failure case --
        # could take the lock, create its worktree, and have THIS run's cleanup
        # delete it out from under it. That is the collision SR3-5 exists to
        # prevent, reintroduced inside SR3-5's own fix. Measured: 1.24s to
        # remove an 827-file worktree on C:, longer across the X: 9p mount.
        if ($override) { Remove-Item -LiteralPath $override -Force -ErrorAction SilentlyContinue }
        if ($src) { try { Remove-CleanSource -Repo $cfg.Repo -Dir $src } catch { } }
        # Only now is nothing SHA-derived left to destroy. Released last.
        if ($haveDeployLock -and $deployMutex) { try { $deployMutex.ReleaseMutex() } catch { } }
        if ($deployMutex) { try { $deployMutex.Dispose() } catch { } }
    }
}

function New-Result {
    param([hashtable]$Cfg)
    return [pscustomobject]@{
        Verdict = $script:D.verdict
        Ledger  = $script:D
    }
}
