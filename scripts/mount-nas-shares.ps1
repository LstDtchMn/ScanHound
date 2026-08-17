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

# --- pinned executables ----------------------------------------------------
# Resolved by ABSOLUTE PATH, never by name. This runs as an elevated Scheduled
# Task 288 times a day; resolving `wsl` or `docker` through PATH means anything
# that can prepend a directory to PATH -- or drop a wsl.bat in an earlier one --
# chooses what runs elevated. Both directories below are writable only by
# TrustedInstaller/SYSTEM/Administrators (verified 2026-07-26).
$WslExe    = Join-Path $env:SystemRoot 'System32\wsl.exe'
$DockerExe = 'C:\Program Files\Docker\Docker\resources\bin\docker.exe'

# Write-shaped rights, tested with ATOMIC flags only.
#
# The first version of this OR'd the COMPOSITE rights (Write|Modify|FullControl)
# into a mask and AND-ed against it. That is wrong: Modify and FullControl
# CONTAIN the read bits, so `ReadAndExecute -band Modify` is non-zero and every
# read-only ACE matched. The check therefore said "writable" about everything,
# including BUILTIN\Users' normal ReadAndExecute on C:\Windows\System32 -- a
# check that answers yes to all inputs is not a check, and it would have made
# the installer fail on its own correctly-hardened deploy directory.
function Test-WriteShapedRight([Security.AccessControl.FileSystemRights]$rights) {
    $R = [Security.AccessControl.FileSystemRights]
    $mask = [int]($R::WriteData -bor $R::AppendData -bor $R::WriteExtendedAttributes -bor
                  $R::WriteAttributes -bor $R::Delete -bor $R::DeleteSubdirectoriesAndFiles -bor
                  $R::ChangePermissions -bor $R::TakeOwnership)
    return (([int]$rights -band $mask) -ne 0)
}

function Assert-PinnedExe([string]$path, [string]$label) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "$label not found at the pinned path '$path'."
    }
    $item = Get-Item -LiteralPath $path -Force
    if ($item.Extension -ne '.exe') {
        throw "$label at '$path' is not an .exe (found '$($item.Extension)')."
    }
    # The EXECUTABLE and its whole directory chain, not just the immediate
    # parent's allow ACEs. A file can retain an explicit write ACE inside an
    # otherwise-protected directory, an unexpected owner implicitly holds
    # WRITE_DAC, and a junction anywhere up the chain redirects the "pinned"
    # path to somewhere that was never checked.
    $trusted = @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM', 'NT SERVICE\TrustedInstaller')

    # Walk the executable and its ancestors under the STRICT mask, then check
    # the volume root separately under RELAXED semantics.
    #
    # Measured: C:\ grants Authenticated Users AppendData. On a directory that
    # bit is CreateDirectories -- permission to make a new top-level folder,
    # not to alter C:\Windows or anything already under it. Applying the strict
    # file mask there rejected every executable on a normal install (verified:
    # wsl.exe, docker.exe and powershell.exe all failed), and a check that
    # rejects the correct configuration is as useless as one that accepts
    # everything.
    #
    # But skipping the root entirely would miss grants that genuinely matter
    # there -- Delete, DeleteSubdirectoriesAndFiles, ChangePermissions,
    # TakeOwnership, WriteAttributes, WriteExtendedAttributes -- so the root is
    # checked with only the two child-creation bits permitted.
    $root     = [IO.Path]::GetPathRoot($item.FullName)
    $rootItem = Get-Item -LiteralPath $root -Force -ErrorAction Stop
    if ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "${label}: volume root '$root' is a reparse point."
    }
    $R = [Security.AccessControl.FileSystemRights]
    # What would actually be dangerous AT THE ROOT: deleting or re-permissioning
    # entries in it, which reaches C:\Windows. Creating a new sibling folder
    # does not.
    $rootDangerous = [int]($R::Delete -bor $R::DeleteSubdirectoriesAndFiles -bor
                           $R::ChangePermissions -bor $R::TakeOwnership)
    foreach ($ace in (Get-Acl -LiteralPath $root).Access) {
        if ($ace.AccessControlType -ne 'Allow') { continue }
        if ($trusted -contains $ace.IdentityReference.Value) { continue }

        # INHERIT-ONLY ACEs grant nothing on the object itself; they are
        # templates applied to children when those are created. Measured on
        # this host: every alarming-looking root ACE (CREATOR OWNER GENERIC_ALL,
        # Authenticated Users 0xE0010000) is InheritOnly. Counting them made
        # the check reject wsl.exe, docker.exe and powershell.exe -- the second
        # time a root check rejected the correct configuration.
        if (($ace.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -ne 0) {
            continue
        }
        # AppContainer capability SIDs (S-1-15-*) are sandboxed package
        # identities, not user principals, and are not the threat model here.
        if ($ace.IdentityReference.Value -like 'S-1-15-*') { continue }

        if (([int]$ace.FileSystemRights -band $rootDangerous) -ne 0) {
            throw ("${label}: volume root '$root' grants '$($ace.FileSystemRights)' to " +
                   "'$($ace.IdentityReference)' -- delete/permission rights at the root " +
                   "reach the protected directories below it.")
        }
    }

    $cur = $item.FullName
    while ($cur -and $cur.TrimEnd('\') -ne $root.TrimEnd('\')) {
        $node = Get-Item -LiteralPath $cur -Force -ErrorAction Stop
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "${label}: '$cur' in the path of '$path' is a reparse point."
        }
        $acl = Get-Acl -LiteralPath $cur
        if ($trusted -notcontains $acl.Owner) {
            throw "${label}: '$cur' is owned by '$($acl.Owner)', which implicitly controls its DACL."
        }
        foreach ($ace in $acl.Access) {
            if ($ace.AccessControlType -ne 'Allow') { continue }
            if ($trusted -contains $ace.IdentityReference.Value) { continue }
            if (Test-WriteShapedRight $ace.FileSystemRights) {
                throw ("${label}: '$cur' grants '$($ace.FileSystemRights)' to " +
                       "'$($ace.IdentityReference)', so '$path' is not effectively pinned.")
            }
        }
        $parent = Split-Path $cur -Parent
        if (-not $parent -or $parent -eq $cur) { break }
        $cur = $parent
    }
}

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
# ---- RUN LOG -------------------------------------------------------------
# This task fires every 12 minutes under wscript.exe via run-hidden.vbs, which
# exists precisely so NO console is allocated. That means every line this script
# prints -- including the RESULT: line naming which share failed -- goes nowhere.
# On 2026-08-16 the task had been returning exit 2 roughly 288 times a day and
# had recorded its reason exactly zero times, so the only way to find out was to
# run it by hand in a visible shell.
#
# WRAPPED IN try/catch AND NEVER FATAL. A log write must not be able to kill the
# mount: an earlier incident in this codebase had a log write under fail-fast
# terminate a job at line 1 and erase its own evidence. If logging fails, the
# mount still runs and the failure is simply unrecorded -- the status quo, not a
# regression.
$MountLog = 'C:\ProgramData\ScanHound\logs\mount-nas-shares.log'
function Write-RunLog([string]$message) {
    try {
        $dir = Split-Path -Parent $MountLog
        if (-not (Test-Path -LiteralPath $dir)) {
            New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
        }
        $line = (Get-Date -Format 's') + '  ' + $message
        Add-Content -LiteralPath $MountLog -Value $line -Encoding utf8 -ErrorAction Stop
        # Keep it bounded: this runs 120x/day and nothing else prunes it.
        $f = Get-Item -LiteralPath $MountLog -ErrorAction Stop
        if ($f.Length -gt 2MB) {
            $keep = Get-Content -LiteralPath $MountLog -Tail 2000 -ErrorAction Stop
            Set-Content -LiteralPath $MountLog -Value $keep -Encoding utf8 -ErrorAction Stop
        }
    } catch { }
}
Write-RunLog "=== run start (pid $PID) ==="

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
    Write-RunLog "FAIL($code): $message"
    exit $code
}

try {

# Fail before doing anything if a pinned executable is missing, is not a real
# .exe, is a reparse point, or sits in a user-writable directory.
Assert-PinnedExe $WslExe    'wsl.exe'
Assert-PinnedExe $DockerExe 'docker.exe'

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
EXPECTED="$2"
EXPECTED_KEYS="$3"   # space-separated; keys are delimiter-safe by construction
CRITICAL_KEY="nas-tv-blackbeard"
fail=0
critical_fail=0
processed=0
critical_seen=0
seen_keys=""

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
# `|| [ -n "$key" ]` is load-bearing. POSIX read returns NON-ZERO when it hits
# EOF on a final line with no terminating newline, having already assigned the
# variables -- so a plain `while read` SILENTLY DROPS THE LAST RECORD. That
# record is nas-tv-blackbeard, the critical read-write TV destination, because
# it is last in the ordered share list. Reproduced 2026-07-26 in bash, dash and
# the docker-desktop distro's BusyBox ash: eight shares verified, the ninth
# never examined, and the script still printed success and exited 0.
while IFS="$(printf '\t')" read -r key share || [ -n "$key" ]; do
    [ -n "$key" ] || continue
    processed=$((processed + 1))

    # MEMBERSHIP, not just cardinality. A count-only check passes when one
    # expected record is omitted and another is duplicated or substituted --
    # the count matches while a real share went unexamined. Reject anything
    # unexpected, reject repeats, and record what was actually seen so the
    # exact expected set can be proven at the end.
    case " $EXPECTED_KEYS " in
        *" $key "*) ;;
        *) printf '  %-32s FAILED (unexpected key not in the expected set)\n' "$key"
           fail=1; critical_fail=1; continue ;;
    esac
    case " $seen_keys " in
        *" $key "*) printf '  %-32s FAILED (duplicate record)\n' "$key"
                    fail=1; critical_fail=1; continue ;;
    esac
    seen_keys="$seen_keys $key"
    [ "$key" = "$CRITICAL_KEY" ] && critical_seen=1

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

# Coverage assertion, in three parts. Count alone is not enough: it is
# satisfied by an omission paired with a duplicate or substitution. Membership
# and uniqueness are enforced in the loop; what remains is proving that EVERY
# expected key was actually seen. Treated as CRITICAL throughout, because an
# unexamined share may BE the critical one and we cannot know which.
missing=""
for want in $EXPECTED_KEYS; do
    case " $seen_keys " in
        *" $want "*) ;;
        *) missing="$missing $want" ;;
    esac
done
if [ -n "$missing" ]; then
    echo "RESULT: coverage FAILED -- expected share(s) never examined:$missing"
    exit 2
fi
if [ "$processed" -ne "$EXPECTED" ]; then
    echo "RESULT: coverage FAILED -- examined $processed records, expected $EXPECTED."
    exit 2
fi
if [ $critical_seen -eq 0 ]; then
    echo "RESULT: coverage FAILED -- the critical share $CRITICAL_KEY was never examined."
    exit 2
fi

if [ $critical_fail -ne 0 ]; then
    echo "RESULT: the CRITICAL read-write share failed"
    exit 2
fi
if [ $fail -ne 0 ]; then
    echo "RESULT: one or more read-only shares failed"
    exit 1
fi
echo "RESULT: all $processed shares mounted and identity-verified"
exit 0
'@

# These two files are handed to wsl.exe, which executes them AS ROOT inside the
# distro. A predictable path under %TEMP% was therefore a swap window.
#
# The FIRST attempt at this granted the current user Full Control and then
# treated that user as trusted while verifying -- which defended against every
# principal EXCEPT the one in the stated threat model. The documented attacker
# is "an unelevated process running as the same user, while the task runs
# elevated", and an allow-ACE for that user's SID does not distinguish the
# elevated token from the same user's medium-integrity process. Reproduced:
# an unelevated process replaced the staged payload with the grant in place.
#
# Ownership matters for the same reason it did for the deploy directory: an
# owner can rewrite the DACL, so a directory owned by the task user is not
# protected by its own grants.
#
# Correct boundary: Administrators + SYSTEM write; the task user gets READ ONLY
# (it must still read nothing here -- wsl.exe reads under the elevated token) --
# and the owner is Administrators. An unelevated same-user process can then
# neither modify the payload nor re-grant itself the right to.
$RunRoot = Join-Path $env:ProgramData 'ScanHound\run'

function Assert-NoReparsePoint([string]$path, [string]$label) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "$label '$path' is a reparse point. A junction/symlink here redirects a trusted path."
    }
}

# Every path component beneath a hardened root must itself be an ordinary
# directory: a lexical "inside the root" check is meaningless if a component is
# a junction pointing elsewhere.
function Assert-NoReparseInChain([string]$path, [string]$root, [string]$label) {
    $cur = (Resolve-Path -LiteralPath $path).ProviderPath
    $end = (Resolve-Path -LiteralPath $root).ProviderPath.TrimEnd('\')
    while ($cur -and $cur.Length -ge $end.Length) {
        Assert-NoReparsePoint $cur $label
        if ($cur.TrimEnd('\') -eq $end) { break }
        $cur = Split-Path $cur -Parent
    }
}

function Assert-AdminOwnedNoUserWrite([string]$path, [string]$label) {
    $trusted = @('BUILTIN\Administrators', 'NT AUTHORITY\SYSTEM', 'NT SERVICE\TrustedInstaller')
    $acl = Get-Acl -LiteralPath $path
    if ($trusted -notcontains $acl.Owner) {
        throw "$label '$path' is owned by '$($acl.Owner)'; an owner can rewrite its DACL."
    }
    foreach ($ace in $acl.Access) {
        if ($ace.AccessControlType -ne 'Allow') { continue }
        if ($trusted -contains $ace.IdentityReference.Value) { continue }
        if (Test-WriteShapedRight $ace.FileSystemRights) {
            throw ("$label '$path' grants '$($ace.FileSystemRights)' to " +
                   "'$($ace.IdentityReference)' -- an unelevated process could swap a payload " +
                   "this script executes as root.")
        }
    }
}

function Initialize-SecureRunRoot {
    $elevated = (New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

    if (-not (Test-Path -LiteralPath $RunRoot)) {
        if (-not $elevated) {
            throw ("Staging root $RunRoot does not exist and creating it securely requires " +
                   "elevation. Run install-mount-task.ps1 elevated once, or run this script " +
                   "from an elevated shell.")
        }
        New-Item -ItemType Directory -Force $RunRoot | Out-Null
    }
    Assert-NoReparsePoint $RunRoot 'Staging root'

    if ($elevated) {
        foreach ($a in @(@('/setowner','BUILTIN\Administrators'), @('/inheritance:r'),
                         @('/grant','BUILTIN\Administrators:(OI)(CI)(F)'),
                         @('/grant','*S-1-5-18:(OI)(CI)(F)'))) {
            & icacls.exe $RunRoot @a /q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls failed hardening $RunRoot ($($a -join ' '))." }
        }
    }
    # Asserted whether or not we just applied it: a pre-existing hostile or
    # merely unhardened root must stop the run.
    Assert-AdminOwnedNoUserWrite $RunRoot 'Staging root'
}

# Windows does NOT inherit an object's owner from its parent directory -- a new
# file's owner comes from the creating token's default owner. An elevated
# admin token usually yields BUILTIN\Administrators, but "usually" is not a
# security property, and an owner implicitly holds WRITE_DAC: it can re-grant
# itself write access even where no write ACE exists. So every staged payload
# is created empty, given an explicit admin-only descriptor, and PROVEN before
# any content is written into it.
function New-SecureStagedFile([string]$path) {
    if (Test-Path -LiteralPath $path) {
        Remove-Item -LiteralPath $path -Force
    }
    # CreateNew: fail rather than reuse anything that appeared underneath us.
    $fs = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $fs.Dispose()

    foreach ($a in @(@('/setowner','BUILTIN\Administrators'), @('/inheritance:r'),
                     @('/grant','BUILTIN\Administrators:(F)'),
                     @('/grant','*S-1-5-18:(F)'))) {
        & icacls.exe $path @a /q | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls failed hardening staged file $path ($($a -join ' '))." }
    }
    Assert-NoReparsePoint $path 'Staged file'
    Assert-AdminOwnedNoUserWrite $path 'Staged file'
    return $path
}

function New-SecureStagingDir {
    Initialize-SecureRunRoot
    $dir = Join-Path $RunRoot ([Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force $dir | Out-Null
    try {
        foreach ($a in @(@('/setowner','BUILTIN\Administrators'), @('/inheritance:r'),
                         @('/grant','BUILTIN\Administrators:(OI)(CI)(F)'),
                         @('/grant','*S-1-5-18:(OI)(CI)(F)'))) {
            & icacls.exe $dir @a /q | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "icacls failed hardening $dir ($($a -join ' '))." }
        }
        Assert-NoReparseInChain $dir $RunRoot 'Staging path'
        Assert-AdminOwnedNoUserWrite $dir 'Staging directory'
        return $dir
    } catch {
        # Cleanup belongs here too: if hardening throws, the caller never
        # receives the path, so the outer finally cannot remove it.
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        throw
    }
}

$stagingDir = New-SecureStagingDir
$tempData   = New-SecureStagedFile (Join-Path $stagingDir "mount-nas.data")
$tempScript = New-SecureStagedFile (Join-Path $stagingDir "mount-nas.sh")
# The data file MUST end with a newline. Without it POSIX `read` returns
# non-zero on the final record and the consuming loop drops it -- silently, and
# always the last share, which is the critical read-write TV destination. The
# shell loops are now independently robust to this and assert their coverage,
# but the input is written correctly in the first place.
(($dataLines -join "`n") + "`n") | Out-File -FilePath $tempData -Encoding ascii -NoNewline
($hostScript -replace "`r`n", "`n") | Out-File -FilePath $tempScript -Encoding ascii -NoNewline

function ConvertTo-WslPath([string]$winPath) {
    $drive = $winPath.Substring(0, 1).ToLower()
    $rest  = $winPath.Substring(2).Replace('\', '/')
    return "/mnt/host/$drive$rest"
}

# --- container probe -------------------------------------------------------

$probeScript = @'
#!/bin/sh
# Verify from INSIDE the container that each bind mount resolves to the
# intended 9p share -- not to an empty local directory that merely looks like
# one. Same identity rule as the host side.
DATA="$1"
EXPECTED="$2"
EXPECTED_TARGETS="$3"
CRITICAL_TARGET="/library/tv"
processed=0
critical_seen=0
seen_targets=""
bad=0
critical_bad=0

# `|| [ -n "$target" ]`: see the host-side note. Without it the final record --
# /library/tv, the critical destination -- is silently never examined, which
# left critical_bad at 0 and made the "identity verified" gate below vacuous.
while IFS="$(printf '\t')" read -r target share || [ -n "$target" ]; do
    [ -n "$target" ] || continue
    processed=$((processed + 1))
    # Membership + uniqueness, same reasoning as the host stage.
    case " $EXPECTED_TARGETS " in
        *" $target "*) ;;
        *) echo "UNEXPECTED $target (not in the expected target set)"
           bad=1; critical_bad=1; continue ;;
    esac
    case " $seen_targets " in
        *" $target "*) echo "DUPLICATE $target"; bad=1; critical_bad=1; continue ;;
    esac
    seen_targets="$seen_targets $target"
    [ "$target" = "$CRITICAL_TARGET" ] && critical_seen=1
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
# `critical_seen` is the fix for a vacuous guard. Gating only on
# `critical_bad -ne 0` asked "did the identity check FAIL?", which is answered
# "no" both when the check passed AND when it never ran. On 2026-07-26 it never
# ran (dropped final record), so this branch wrote into an UNVERIFIED
# /library/tv -- a local ext4 directory -- succeeded, and printed OK. Requiring
# proof that the check actually ran turns "unknown" back into "not verified".
if [ $critical_seen -eq 0 ]; then
    echo "SKIP  $CRITICAL_TARGET write probe (target was never examined)"
    bad=1; critical_bad=1
elif [ $critical_bad -ne 0 ]; then
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

# Same three-part coverage assertion as the host stage.
missing=""
for want in $EXPECTED_TARGETS; do
    case " $seen_targets " in
        *" $want "*) ;;
        *) missing="$missing $want" ;;
    esac
done
if [ -n "$missing" ]; then
    echo "COVERAGE FAILED -- target(s) never examined:$missing"
    exit 2
fi
if [ "$processed" -ne "$EXPECTED" ]; then
    echo "COVERAGE FAILED -- examined $processed records, expected $EXPECTED"
    exit 2
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

# Same staging directory, same reason: these are docker-cp'd into the container
# and executed there, so a predictable user-writable source path is a swap
# window. Reuses the hardened per-run directory created above.
$probeScriptPath = New-SecureStagedFile (Join-Path $stagingDir "probe-mounts.sh")
$probeDataPath   = New-SecureStagedFile (Join-Path $stagingDir "probe-mounts.data")
($probeScript -replace "`r`n", "`n") | Out-File -FilePath $probeScriptPath -Encoding ascii -NoNewline
# Trailing newline required -- see the host-side note. The dropped record here
# was /library/tv, which made the write-probe's identity gate vacuous.
(($probeData -join "`n") + "`n")     | Out-File -FilePath $probeDataPath   -Encoding ascii -NoNewline

# Bounded: a wedged Docker daemon or container makes `docker exec` HANG rather
# than return, which would leave the Scheduled Task running forever. Distinguish
# the outcomes -- "empty output" is not a diagnosis.
$ExpectedTargets    = $probeData.Count
# Container targets are also delimiter-safe (/library/... with no spaces).
$ExpectedTargetList = (($shares.Keys | ForEach-Object { Get-ContainerTarget $_ }) -join ' ')

function Invoke-ContainerProbe([int]$TimeoutSec = 90) {
    $result = [pscustomobject]@{ Code = -1; Output = ""; Reason = "" }

    $running = & $DockerExe ps --filter "name=^scanhound$" --format "{{.Names}}" 2>$null
    if ($LASTEXITCODE -ne 0) { $result.Reason = "docker-unavailable"; return $result }
    if (-not $running)       { $result.Reason = "not-running";        return $result }

    & $DockerExe cp $probeScriptPath scanhound:/tmp/scanhound-probe-mounts.sh 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { $result.Reason = "copy-failed"; return $result }
    & $DockerExe cp $probeDataPath scanhound:/tmp/scanhound-probe-mounts.data 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) { $result.Reason = "copy-failed"; return $result }

    # -ArgumentList, because a Start-Job scriptblock does not inherit the
    # caller's scope: $ExpectedTargets would silently be $null inside it, and
    # the probe's coverage assertion would then compare against an empty
    # string and fail every run.
    # $DockerExe is passed in explicitly: a Start-Job scriptblock runs in a
    # SEPARATE PowerShell process that inherits neither the caller's variables
    # nor a trustworthy PATH, so a bare `docker` here would silently reopen the
    # command-resolution hole the pinning closes everywhere else.
    $job = Start-Job -ArgumentList $ExpectedTargets, $DockerExe, $ExpectedTargetList -ScriptBlock {
        param($expected, $dockerExe, $expectedList)
        $out = & $dockerExe exec scanhound sh /tmp/scanhound-probe-mounts.sh `
                   /tmp/scanhound-probe-mounts.data $expected $expectedList 2>&1
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
    # $DockerExe MUST be passed in. A Start-Job scriptblock runs in a separate
    # PowerShell process and inherits none of the caller's variables, so the
    # bare reference this replaced expanded to $null -- i.e. the safety stop
    # invoked nothing at all. Verified empirically: $DockerExe is empty inside
    # a job. The outer `docker ps` check meant the failure was reported honestly
    # rather than claimed as a stop, but the container was never stopped.
    $job = Start-Job -ArgumentList $DockerExe -ScriptBlock {
        param($dockerExe)
        & $dockerExe stop -t 20 scanhound 2>&1 | Out-Null
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
    $still = & $DockerExe ps --filter "name=^scanhound$" --format "{{.Names}}" 2>$null
    if ($LASTEXITCODE -ne 0) { return "docker-unavailable" }
    if ($still)              { return "stop-failed" }
    if ($code -ne 0)         { return "stopped-despite-error" }
    return "stopped"
}

Write-Host "Mounting NAS shares inside the docker-desktop WSL2 distro..."
# The expected record count is passed in so the shell can assert it examined
# every share. Derived from the same $shares list that produced the data file,
# so the two can never disagree about how many there should be.
#
# IN-DISTRO TIMEOUT. Killing wsl.exe on the Windows side does not prove the
# Linux-side shell stopped, and Task Scheduler's ExecutionTimeLimit is only an
# outer watchdog over the Windows task host -- it has NOT been shown to reach
# into the VM. Without this, a wedged mount could outlive its invocation and
# still be running when the 5-minute repeater fires again.
#
# BusyBox timeout, not GNU: verified on this host it takes `-s SIG -k SECS SECS`
# (no --long-options, no `s` suffix) and exits 15 on timeout -- distinct from
# this script's own 0/1/2, so a timeout can never be mistaken for a verdict.
# 240 s leaves wide margin under the PT15M outer limit.
$MountTimeoutSec = 240
# Keys are [a-z0-9-] by construction, so a space-separated list is unambiguous
# and needs no quoting through the PowerShell -> wsl.exe -> sh layers.
$ExpectedKeys = ($shares.Keys -join ' ')
& $WslExe -d docker-desktop -- timeout -s TERM -k 5 $MountTimeoutSec `
    sh (ConvertTo-WslPath $tempScript) (ConvertTo-WslPath $tempData) $shares.Count $ExpectedKeys
$mountExit = $LASTEXITCODE

# ALLOWLIST, not a special case for the one timeout code that was measured.
# The host shell's verdict space is exactly {0 = all verified, 1 = read-only
# share failed, 2 = critical/coverage failure}. Anything else -- 15 from a
# BusyBox TERM timeout, 137 from a SIGKILL escalation, 126/127 from a failed
# exec, a WSL interruption -- is an INFRASTRUCTURE outcome, not a statement
# about the shares.
#
# Treating only 15 as indeterminate left every other value falling through to
# ordinary handling, where `$criticalHostFailure = ($mountExit -eq 2)` is
# false. A blind container could then be recreated on the strength of a result
# that never described the mounts at all. An unrecognised result must never
# authorise a recreate.
if ($mountExit -notin @(0, 1, 2)) {
    $why = if ($mountExit -eq 15) { "exceeded $MountTimeoutSec s and was terminated" }
           else { "returned $mountExit, which is not one of its defined verdicts (0/1/2)" }
    Write-Host "The in-distro mount stage $why."
    Write-Host "Treating this as INDETERMINATE: no recreate will be attempted from an unknown result."

    # The container may still be running against local directories, so the
    # critical destination has to be proven independently or the container
    # stopped -- exactly the critical path, reached without a share verdict.
    $ind = Invoke-ContainerProbe
    if ($ind.Reason -eq "probed" -and $ind.Code -eq 0) {
        Fail ("Mount stage indeterminate, but the running container still proves all nine " +
              "targets including $CriticalTarget. Left running; NOT recreated. The scheduler " +
              "will retry.") 8
    }
    Write-Host "Container cannot prove $CriticalTarget after an indeterminate mount stage."
    $stopState = Stop-ScanhoundVerified
    Write-Host "Stop result: $stopState"
    if ($stopState -eq "stopped" -or $stopState -eq "stopped-despite-error" -or $stopState -eq "not-running") {
        Fail ("Mount stage indeterminate and $CriticalTarget unproven; the container is not " +
              "running, so nothing can write into a non-NAS directory. Scheduler will retry.") 8
    }
    Fail ("Mount stage indeterminate, $CriticalTarget unproven, and the container could not be " +
          "confirmed stopped. MANUAL INTERVENTION REQUIRED.") 7
}

# The whole staging directory is removed in the finally block, which also
# covers the early-exit paths this line never reached.
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

    # --no-build and --pull never close the last mutable-source tail. Pinning
    # the recipe alone is not sufficient: the service declares BOTH
    # `build: .` and `image: scanhound:latest`, and --project-directory still
    # points at the working tree, so a MISSING local image would let Compose
    # build a recovery-time image from whatever branch happens to be checked
    # out. --pull never additionally stops a registry image from silently
    # replacing the locally validated one.
    #
    # Recovery must only ever restart what was already reviewed and built.
    # A missing image is a deployment problem for a human, not something a
    # recovery script should paper over.
    & $DockerExe compose -f $ComposeFile --project-directory $ComposeProjectDir `
        up -d --force-recreate --no-build --pull never
    if ($LASTEXITCODE -ne 0) {
        Fail ("docker compose up -d --force-recreate --no-build --pull never failed " +
              "(exit $LASTEXITCODE). If the scanhound:latest image is missing, that is " +
              "deliberate: recovery never builds from the working tree.") 4
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
    Write-Host "Post-recreate verification passed: all $ExpectedTargets targets identity-verified."
}

if ($mountExit -ne 0) {
    Fail "One or more read-only shares are still unavailable (see the log above)." 1
}

Write-Host "Done."
Write-RunLog "OK: all shares mounted and identity-verified"
exit 0

}
finally {
    # Cleanup belongs here, not on the success path: `Fail` exits, and every
    # early exit would otherwise leave the staged root-executed payloads on
    # disk. PowerShell runs finally on `exit` from inside try, so this covers
    # all of them.
    if ($stagingDir -and (Test-Path -LiteralPath $stagingDir)) {
        Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($haveLock) {
        $mutex.ReleaseMutex()
        $mutex.Dispose()
    }
}
