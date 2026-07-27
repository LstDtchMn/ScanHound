# Re-establishes the 9 TURTLELANDSRV2 NAS share mounts inside Docker
# Desktop's internal WSL2 distro (docker-desktop), so docker-compose.yml's
# bind mounts under /mnt/nas/... (see the volumes: block) have something real
# to mount from.
#
# WHY THIS EXISTS: Docker Desktop cannot bind-mount a raw UNC path
# (\\TURTLELANDSRV2\...) via docker-compose in any form -- confirmed
# 2026-07-12 against both the short-form and long-form volume syntax, both
# rejected with "...is not a valid Windows path" (a real Docker Desktop
# path-validator bug, not a syntax mistake). Even an existing, working
# net-use drive-letter mapping fails too, because Docker's backend service
# runs in a different session context that can't see per-user net-use
# mappings.
#
# The workaround is to mount each share directly inside the docker-desktop
# WSL2 distro itself via `mount -t drvfs`, which uses the same transparent
# Windows authentication Explorer already has for this NAS (no credentials
# needed or stored anywhere). That produces a plain Linux path
# (/mnt/nas/<share>) that Docker CAN bind-mount normally.
#
# CAVEAT: these WSL2-level mounts do NOT survive a Docker Desktop restart,
# WSL2 shutdown, or host reboot -- this script re-establishes them and is
# idempotent, so it is safe to re-run at any time.
#
# ---------------------------------------------------------------------------
# WHAT THIS SCRIPT PROVES, AND WHY IT IS PARANOID
#
# A 2026-07-26 outage had all nine shares unmounted while the Scheduled Task
# reported LastTaskResult 0. The container was bound to empty directories and
# the whole NAS library -- plus /library/tv, the read-WRITE TV destination --
# was invisible. The lesson is that "it looked fine" is not evidence.
#
# So this script never infers a mount from directory contents. A directory
# containing files proves nothing: if a process wrote into the ordinary
# underlying /mnt/nas/<key> directory while the share was absent, a
# contents-based check would report "already mounted" forever and the
# container would keep reading and writing LOCAL VM files. That is the exact
# failure class this script exists to prevent, so mount IDENTITY is proven
# first, from /proc/self/mountinfo:
#
#   * the target is a real mountpoint;
#   * its filesystem type is 9p;
#   * its superblock options carry path=UNC\TURTLELANDSRV2\<expected share>.
#
# Only once identity holds is the share checked for content (liveness). The
# same identity check runs INSIDE the container after any recreate, because
# compose exiting 0 proves a container started -- not that its bind mounts
# resolve to the intended shares.
# ---------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

# Share key -> Windows share name under \\TURTLELANDSRV2
$shares = [ordered]@{
    "nas-1080p-john-paul-jones"    = "1080p John Paul Jones"
    "nas-1080p-lincoln"            = "1080p Lincoln"
    "nas-1080p-faraday"            = "1080p Faraday"
    "nas-1080p-icarus"             = "1080p Icarus"
    "nas-1080p-nathan-hale"        = "1080p Nathan Hale"
    "nas-1080p-picasso-aka-newton" = "1080p Picasso aka Newton"
    "nas-4k-hdr-geronimo"          = "4K HDR Geronimo"
    "nas-4k-magellan"              = "4K Magellan"
    # TV Shows Blackbeard (the share name is literally "k"; V: on the host).
    # Unlike the eight read-only Plex sources, this one is bind-mounted
    # READ-WRITE: it is the TV download, extraction and rename destination.
    # That makes it the critical share -- see $CriticalKey below.
    "nas-tv-blackbeard"            = "k"
}

# The read-write destination. If this share is not mounted, correct and
# writable, the application must NOT be left running: it would write TV files
# into a local VM directory, silently, where Plex will never see them.
$CriticalKey    = "nas-tv-blackbeard"
$CriticalTarget = "/library/tv"

function Get-ContainerTarget([string]$key) {
    if ($key -eq $CriticalKey) { return $CriticalTarget }
    return "/library/plex-source/$key"
}

# --- stable Compose inputs -------------------------------------------------
# This script may recreate the container. Reading docker-compose.yml from the
# working tree meant a branch checkout could silently change WHICH service
# definition a recovery run deployed -- volumes, networks, restart policy --
# without review. Pin the recipe to the deployed, hashed copy instead.
#
# --project-directory still points at the working tree because the compose
# file's relative paths (./data) must resolve to the LIVE application data
# directory, which is gitignored and therefore unaffected by branch checkouts.
# Only the recipe is pinned; the data it names is deliberately not moved.
$ComposeFile       = "C:\ProgramData\ScanHound\deploy\docker-compose.yml"
$ComposeProjectDir = "X:\Docker Apps\ScanHound"

# --- single instance -------------------------------------------------------
# The Scheduled Task is registered At Logon AND At Startup, which can overlap.
# Two concurrent runs would unmount shares under each other and race on the
# container recreate. A named system-wide mutex serializes them; a second
# instance exits 0 without touching anything, because "someone else is already
# doing this" is not a failure.
$mutex = New-Object System.Threading.Mutex($false, "Global\ScanHound-MountNASShares")
$haveLock = $false
try {
    $haveLock = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # A previous holder died without releasing. We now own it.
    $haveLock = $true
}
if (-not $haveLock) {
    Write-Host "Another instance is already running; exiting without touching mounts."
    exit 0
}

function Fail([string]$message, [int]$code) {
    # NOT Write-Error: under $ErrorActionPreference = "Stop" that is a
    # TERMINATING error, so the `exit` below would never run and the intended
    # native exit code would not reach the Scheduled Task's LastTaskResult.
    [Console]::Error.WriteLine("ERROR: $message")
    exit $code
}

try {

# --- host side: mount + prove identity inside the WSL2 distro ---------------

# Passed as a data file rather than generated per-share, and read with a tab
# IFS: share names contain spaces, and multi-layer PowerShell -> wsl.exe ->
# Linux-shell quoting silently truncates them (confirmed 2026-07-12).
$dataLines = @()
foreach ($key in $shares.Keys) { $dataLines += "$key`t$($shares[$key])" }

$hostScript = @'
#!/bin/sh
# Mount each share and PROVE it, or fail loudly. Never infers a mount from
# directory contents. Exit 0 only if every share is identity-verified.
DATA="$1"
CRITICAL_KEY="nas-tv-blackbeard"
fail=0
critical_fail=0

# 0 = mounted and correct, 1 = not a mountpoint, 2 = wrong fs type,
# 3 = mounted but a DIFFERENT share
verify_identity() {
    _key="$1"; _share="$2"; _target="/mnt/nas/$1"
    mountpoint -q "$_target" 2>/dev/null || return 1
    _line=$(grep " $_target " /proc/self/mountinfo | tail -1)
    [ -n "$_line" ] || return 1
    _after=${_line#* - }
    _fstype=${_after%% *}
    [ "$_fstype" = "9p" ] || return 2
    # Superblock options carry the true origin, e.g.
    #   path=UNC\TURTLELANDSRV2\4K HDR Geronimo;
    # Anchored with the leading/trailing ';' so one share cannot prefix-match
    # another.
    #
    # Matched with `case`, NOT `echo ... | grep`: in a POSIX sh whose echo
    # interprets backslash escapes (dash does; BusyBox ash does not), the
    # backslash before a share name starting with a DIGIT is read as an octal
    # escape -- \4K and \1080p are destroyed before grep sees them. That made
    # eight of nine shares report "wrong share" while `k` passed, purely
    # because its name is not a digit. `case` does literal substring matching
    # with no escape processing at all.
    _expected=";path=UNC\\TURTLELANDSRV2\\$_share;"
    case "$_line" in
        *"$_expected"*) return 0 ;;
        *) return 3 ;;
    esac
}

echo "=== mounting and verifying NAS shares ==="
while IFS="$(printf '\t')" read -r key share; do
    [ -n "$key" ] || continue
    target="/mnt/nas/$key"
    mkdir -p "$target"

    verify_identity "$key" "$share"
    rc=$?
    if [ $rc -eq 0 ]; then
        n=$(ls "$target" 2>/dev/null | wc -l)
        if [ "$n" -eq 0 ]; then
            # Identity is proven, so this is a real but empty share. These nine
            # are operationally required to hold at least one stable object, so
            # empty means something is wrong upstream.
            printf '  %-32s FAILED (mounted, correct, but EMPTY)\n' "$key"
            fail=1
            [ "$key" = "$CRITICAL_KEY" ] && critical_fail=1
        else
            printf '  %-32s OK (already mounted, verified, %s entries)\n' "$key" "$n"
        fi
        continue
    fi

    if [ $rc -eq 3 ]; then
        # A DIFFERENT share is mounted here. Do not silently remount over it --
        # that hides a configuration error and could unmount something in use.
        printf '  %-32s FAILED (wrong share mounted at this target)\n' "$key"
        fail=1
        [ "$key" = "$CRITICAL_KEY" ] && critical_fail=1
        continue
    fi

    # rc 1 (not a mountpoint) or 2 (wrong fs type): clear and mount.
    umount "$target" 2>/dev/null
    if ! mount -t drvfs "\\\\TURTLELANDSRV2\\$share" "$target" 2>/dev/null; then
        printf '  %-32s FAILED (mount error)\n' "$key"
        fail=1
        [ "$key" = "$CRITICAL_KEY" ] && critical_fail=1
        continue
    fi

    # Re-verify: a mount that returns 0 still has to BE the right share.
    verify_identity "$key" "$share"
    if [ $? -ne 0 ]; then
        printf '  %-32s FAILED (mounted but identity not verified)\n' "$key"
        fail=1
        [ "$key" = "$CRITICAL_KEY" ] && critical_fail=1
        continue
    fi
    n=$(ls "$target" 2>/dev/null | wc -l)
    if [ "$n" -eq 0 ]; then
        printf '  %-32s FAILED (mounted, correct, but EMPTY)\n' "$key"
        fail=1
        [ "$key" = "$CRITICAL_KEY" ] && critical_fail=1
        continue
    fi
    printf '  %-32s OK (mounted and verified, %s entries)\n' "$key" "$n"
done < "$DATA"

if [ $critical_fail -ne 0 ]; then
    echo "RESULT: the CRITICAL read-write share failed"
    exit 2
fi
if [ $fail -ne 0 ]; then
    echo "RESULT: one or more read-only shares failed"
    exit 1
fi
echo "RESULT: all nine shares mounted and identity-verified"
exit 0
'@

$tempData   = Join-Path $env:TEMP "scanhound-mount-nas.data"
$tempScript = Join-Path $env:TEMP "scanhound-mount-nas.sh"
($dataLines  -join "`n") | Out-File -FilePath $tempData   -Encoding ascii -NoNewline
($hostScript -replace "`r`n", "`n") | Out-File -FilePath $tempScript -Encoding ascii -NoNewline

function ConvertTo-WslPath([string]$winPath) {
    $drive = $winPath.Substring(0, 1).ToLower()
    $rest  = $winPath.Substring(2).Replace('\', '/')
    return "/mnt/host/$drive$rest"
}

Write-Host "Mounting NAS shares inside the docker-desktop WSL2 distro..."
wsl -d docker-desktop -- sh (ConvertTo-WslPath $tempScript) (ConvertTo-WslPath $tempData)
$mountExit = $LASTEXITCODE

Remove-Item $tempScript, $tempData -Force -ErrorAction SilentlyContinue

# Partial-failure policy, stated deliberately rather than by omission:
#
#   * critical share failed  -> never touch the container. Recreating would
#     bind /library/tv to a local VM directory and the app would write TV
#     files somewhere Plex cannot see. Hard stop.
#   * only read-only shares failed -> still a failure and still exits nonzero,
#     but whether to recreate depends on what the container can currently see
#     (below). Restoring eight of nine sources beats total blindness; it does
#     NOT beat a healthy container, so a healthy one is left alone.
# Deliberately NOT handled here. "Do not recreate" is correct when the critical
# share is unverified, but "do not touch the container" is NOT safe: if the
# container is already blind (WSL bounced, then the share failed to remount) it
# keeps running with /library/tv bound to a local VM directory and silently
# writes TV files where Plex will never see them. So the critical-failure path
# still PROBES the existing container below, and stops it unless its
# /library/tv is independently identity-verified and writable.
$criticalHostFailure = ($mountExit -eq 2)

# --- container probe -------------------------------------------------------

$probeScript = @'
#!/bin/sh
# Verify from INSIDE the container that each bind mount resolves to the
# intended 9p share -- not to an empty local directory that merely looks like
# one. Same identity rule as the host side.
DATA="$1"
CRITICAL_TARGET="/library/tv"
bad=0
critical_bad=0

while IFS="$(printf '\t')" read -r target share; do
    [ -n "$target" ] || continue
    if ! mountpoint -q "$target" 2>/dev/null; then
        echo "BLIND $target (not a mountpoint)"
        bad=1; [ "$target" = "$CRITICAL_TARGET" ] && critical_bad=1
        continue
    fi
    line=$(grep " $target " /proc/self/mountinfo | tail -1)
    after=${line#* - }
    fstype=${after%% *}
    if [ "$fstype" != "9p" ]; then
        echo "BLIND $target (fstype=$fstype)"
        bad=1; [ "$target" = "$CRITICAL_TARGET" ] && critical_bad=1
        continue
    fi
    # `case`, not `echo | grep` -- see the host-side note: dash's echo eats the
    # backslash before a digit as an octal escape and corrupts the comparison.
    expected=";path=UNC\\TURTLELANDSRV2\\$share;"
    case "$line" in
        *"$expected"*) ;;
        *)
            echo "WRONG $target (unexpected share)"
            bad=1; [ "$target" = "$CRITICAL_TARGET" ] && critical_bad=1
            continue
            ;;
    esac
    echo "OK    $target"
done < "$DATA"

# The critical share must be provably WRITABLE and DELETABLE, not merely
# present. A read-only or stale-handle mount silently breaks every TV rename,
# and this path is a download/extraction/rename destination, so remove()
# semantics matter as much as write().
#
# Gated on identity: never write into a target whose identity check already
# failed. Writing to an unverified /library/tv is exactly the local-VM-directory
# accident this script exists to prevent.
if [ $critical_bad -ne 0 ]; then
    echo "SKIP  $CRITICAL_TARGET write probe (identity not verified)"
else
    probe="$CRITICAL_TARGET/.scanhound-mount-probe.$$"
    rm -f "$probe" 2>/dev/null
    if echo scanhound > "$probe" 2>/dev/null &&
       [ -s "$probe" ] &&
       rm -f "$probe" 2>/dev/null &&
       [ ! -e "$probe" ]; then
        echo "OK    $CRITICAL_TARGET (write+delete probe passed)"
    else
        rm -f "$probe" 2>/dev/null
        echo "UNWRITABLE $CRITICAL_TARGET (write, non-empty, delete or absence check failed)"
        bad=1; critical_bad=1
    fi
fi

[ $critical_bad -ne 0 ] && exit 2
[ $bad -ne 0 ] && exit 1
exit 0
'@

# target<TAB>share, for the container-side check
$probeData = @()
foreach ($key in $shares.Keys) {
    $probeData += "$(Get-ContainerTarget $key)`t$($shares[$key])"
}

$probeScriptPath = Join-Path $env:TEMP "scanhound-probe-mounts.sh"
$probeDataPath   = Join-Path $env:TEMP "scanhound-probe-mounts.data"
($probeScript -replace "`r`n", "`n") | Out-File -FilePath $probeScriptPath -Encoding ascii -NoNewline
($probeData -join "`n")              | Out-File -FilePath $probeDataPath   -Encoding ascii -NoNewline

# Bounded: a wedged Docker daemon or container makes `docker exec` HANG rather
# than return, which would leave the Scheduled Task running forever. Distinguish
# the outcomes -- "empty output" is not a diagnosis.
function Invoke-ContainerProbe([int]$TimeoutSec = 90) {
    $result = [pscustomobject]@{ Code = -1; Output = ""; Reason = "" }

    $running = docker ps --filter "name=^scanhound$" --format "{{.Names}}" 2>$null
    if ($LASTEXITCODE -ne 0) { $result.Reason = "docker-unavailable"; return $result }
    if (-not $running)       { $result.Reason = "not-running";        return $result }

    docker cp $probeScriptPath scanhound:/tmp/scanhound-probe-mounts.sh 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { $result.Reason = "copy-failed"; return $result }
    docker cp $probeDataPath scanhound:/tmp/scanhound-probe-mounts.data 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { $result.Reason = "copy-failed"; return $result }

    $job = Start-Job -ScriptBlock {
        $out = docker exec scanhound sh /tmp/scanhound-probe-mounts.sh `
                   /tmp/scanhound-probe-mounts.data 2>&1
        [pscustomobject]@{ Out = ($out | Out-String); Code = $LASTEXITCODE }
    }
    if (-not (Wait-Job $job -Timeout $TimeoutSec)) {
        Stop-Job $job -ErrorAction SilentlyContinue
        Remove-Job $job -Force -ErrorAction SilentlyContinue
        $result.Reason = "timeout"
        return $result
    }
    $r = Receive-Job $job
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    $result.Output = $r.Out
    $result.Code   = $r.Code
    $result.Reason = "probed"
    return $result
}

# Bounded, checked, VERIFIED stop. Never report "stopped" without proving it:
# `docker stop | Out-Null` hides a failure and an unreachable daemon alike, and
# the caller then asserts a safe state that may not exist.
function Stop-ScanhoundVerified([int]$TimeoutSec = 45) {
    $job = Start-Job -ScriptBlock {
        docker stop -t 20 scanhound 2>&1 | Out-Null
        $LASTEXITCODE
    }
    if (-not (Wait-Job $job -Timeout $TimeoutSec)) {
        Stop-Job $job -ErrorAction SilentlyContinue
        Remove-Job $job -Force -ErrorAction SilentlyContinue
        return "stop-timed-out"
    }
    $code = Receive-Job $job
    Remove-Job $job -Force -ErrorAction SilentlyContinue

    # Independently confirm it is no longer running, whatever stop reported.
    $still = docker ps --filter "name=^scanhound$" --format "{{.Names}}" 2>$null
    if ($LASTEXITCODE -ne 0) { return "docker-unavailable" }
    if ($still)              { return "stop-failed" }
    if ($code -ne 0)         { return "stopped-despite-error" }
    return "stopped"
}

$probe = Invoke-ContainerProbe
Write-Host "Container mount probe: $($probe.Reason)"
if ($probe.Output) { Write-Host $probe.Output.TrimEnd() }

# Critical host mount unverified: never (re)create, but do not leave a possibly
# blind container writing into a local directory either.
if ($criticalHostFailure) {
    $safe = ($probe.Reason -eq "probed" -and $probe.Code -eq 0)
    if ($safe) {
        Fail ("The critical share ($CriticalKey) failed host verification, but the " +
              "running container's $CriticalTarget is independently identity-verified " +
              "and writable. Left running; NOT recreated.") 2
    }
    if ($probe.Reason -eq "not-running") {
        Fail ("The critical share ($CriticalKey) is not mounted and verified. " +
              "Container is not running and was NOT started.") 2
    }
    Write-Host "Critical share unverified and the container is not provably safe -- stopping it."
    $stopState = Stop-ScanhoundVerified
    Write-Host "Stop result: $stopState"
    if ($stopState -eq "stopped" -or $stopState -eq "stopped-despite-error") {
        Fail ("The critical share ($CriticalKey -> $CriticalTarget) is not mounted and " +
              "verified. The container has been STOPPED (verified not running) to " +
              "prevent TV writes into a non-NAS directory.") 2
    }
    Fail ("The critical share ($CriticalKey -> $CriticalTarget) is not mounted and " +
          "verified, AND the container could not be confirmed stopped ($stopState). " +
          "It may still be writing into a non-NAS directory -- intervene manually.") 7
}

$needsRecreate = $false
switch ($probe.Reason) {
    "not-running"        { Write-Host "scanhound is not running -- starting it."; $needsRecreate = $true }
    "timeout"            { Write-Host "Probe timed out (wedged container/daemon) -- recreating."; $needsRecreate = $true }
    "copy-failed"        { Write-Host "Could not copy the probe in -- recreating."; $needsRecreate = $true }
    "docker-unavailable" { Fail "Docker is not available; cannot verify or recreate." 3 }
    "probed"             {
        if ($probe.Code -ne 0) {
            Write-Host "Container cannot see one or more shares -- recreate required."
            $needsRecreate = $true
        } else {
            Write-Host "Container already sees all nine verified mounts -- leaving it running."
        }
    }
}

# A healthy container is never sacrificed for a partial mount failure.
if ($needsRecreate -and $mountExit -ne 0 -and $probe.Reason -eq "probed" -and $probe.Code -eq 0) {
    $needsRecreate = $false
}

if ($needsRecreate) {
    # Fail closed. A recreate without the reviewed recipe is precisely the
    # unreviewed-config-reaches-production path this pinning exists to close,
    # so a missing bundle must stop the recreate rather than fall back to the
    # working tree.
    if (-not (Test-Path -LiteralPath $ComposeFile)) {
        Fail ("Deployed Compose recipe not found at $ComposeFile -- refusing to " +
              "recreate from the mutable working tree. Redeploy the bundle.") 4
    }

    Write-Host "Recreating the scanhound container to pick up live mounts..."
    Write-Host "  compose file: $ComposeFile"
    Write-Host "  project dir : $ComposeProjectDir"
    docker compose -f $ComposeFile --project-directory $ComposeProjectDir up -d --force-recreate
    if ($LASTEXITCODE -ne 0) {
        Fail "docker compose up -d --force-recreate failed (exit $LASTEXITCODE)." 4
    }

    # THE completion gate. Compose exiting 0 proves a container started, not
    # that its nine bind mounts resolve to the intended shares or that the
    # read-write destination is writable.
    Start-Sleep -Seconds 5
    $post = Invoke-ContainerProbe
    Write-Host "Post-recreate verification: $($post.Reason)"
    if ($post.Output) { Write-Host $post.Output.TrimEnd() }

    if ($post.Reason -ne "probed" -or $post.Code -ne 0) {
        if ($post.Reason -ne "probed" -or $post.Code -eq 2) {
            # The critical read-write destination is not proven good. Do not
            # leave the app able to write TV files into a local VM directory.
            Write-Host "Stopping scanhound: the read-write TV destination is not verified."
            $stopState = Stop-ScanhoundVerified
            Write-Host "Stop result: $stopState"
            if ($stopState -eq "stopped" -or $stopState -eq "stopped-despite-error") {
                Fail ("Post-recreate verification failed for $CriticalTarget. The container " +
                      "has been STOPPED (verified not running) to prevent writes into a " +
                      "non-NAS directory.") 5
            }
            Fail ("Post-recreate verification failed for $CriticalTarget AND the container " +
                  "could not be confirmed stopped ($stopState). It may still be writing " +
                  "into a non-NAS directory -- intervene manually.") 7
        }
        Fail "Post-recreate verification failed for one or more read-only sources." 6
    }
    Write-Host "Post-recreate verification passed: all nine targets identity-verified."
}

Remove-Item $probeScriptPath, $probeDataPath -Force -ErrorAction SilentlyContinue

if ($mountExit -ne 0) {
    Fail "One or more read-only shares are still unavailable (see the log above)." 1
}

Write-Host "Done."
exit 0

}
finally {
    if ($haveLock) {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
}
