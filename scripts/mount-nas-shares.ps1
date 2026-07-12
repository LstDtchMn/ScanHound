# Re-establishes the 8 TURTLELANDSRV2 NAS share mounts inside Docker
# Desktop's internal WSL2 distro (docker-desktop), so docker-compose.yml's
# read-only bind mounts under /mnt/nas/... (see the volumes: block) have
# something real to mount from.
#
# WHY THIS EXISTS: Docker Desktop cannot bind-mount a raw UNC path
# (\\TURTLELANDSRV2\...) via docker-compose in any form -- confirmed
# 2026-07-12 against both the short-form and long-form volume syntax, both
# rejected with "...is not a valid Windows path" (a real Docker Desktop
# path-validator bug, not a syntax mistake). Even an existing, working
# net-use drive-letter mapping to one of these shares fails too, because
# Docker's backend service runs in a different session context that can't
# see per-user net-use mappings.
#
# The workaround is to mount each share directly inside the docker-desktop
# WSL2 distro itself via `mount -t drvfs`, which uses the same transparent
# Windows authentication Explorer already has for this NAS (no credentials
# needed or stored anywhere). That produces a plain Linux path
# (/mnt/nas/<share>) that Docker CAN bind-mount normally, since it never
# goes through the broken Windows-path validator at all.
#
# CAVEAT: these WSL2-level mounts do NOT survive a Docker Desktop restart,
# WSL2 shutdown, or host reboot on their own -- this script re-creates them
# from scratch every time it runs (idempotent: safe to re-run any time).
# Register it as a Scheduled Task (At log on / At startup) so it reruns
# automatically; it can also just be run manually on demand.

$ErrorActionPreference = "Stop"

$shares = [ordered]@{
    "nas-1080p-john-paul-jones"    = "1080p John Paul Jones"
    "nas-1080p-lincoln"            = "1080p Lincoln"
    "nas-1080p-faraday"            = "1080p Faraday"
    "nas-1080p-icarus"             = "1080p Icarus"
    "nas-1080p-nathan-hale"        = "1080p Nathan Hale"
    "nas-1080p-picasso-aka-newton" = "1080p Picasso aka Newton"
    "nas-4k-hdr-geronimo"          = "4K HDR Geronimo"
    "nas-4k-magellan"              = "4K Magellan"
}

# Written to a script file rather than passed inline through
# `wsl -d docker-desktop -- sh -c "..."` -- multi-layer PowerShell -> wsl.exe
# -> Linux-shell quoting silently truncates share names containing spaces
# (confirmed 2026-07-12). A script file sidesteps that entirely.
$scriptLines = @("#!/bin/sh", "set -e", "")
foreach ($key in $shares.Keys) {
    $share = $shares[$key]
    $scriptLines += "mkdir -p /mnt/nas/$key"
    $scriptLines += "umount /mnt/nas/$key 2>/dev/null || true"
    $scriptLines += "mount -t drvfs '\\\\TURTLELANDSRV2\$share' /mnt/nas/$key && echo 'OK: $key' || echo 'FAILED: $key'"
}

$tempScript = Join-Path $env:TEMP "scanhound-mount-nas.sh"
($scriptLines -join "`n") | Out-File -FilePath $tempScript -Encoding ascii -NoNewline

# Translate the Windows temp path to the WSL2 drvfs path so the distro can
# read the script file directly (C:\Users\... -> /mnt/host/c/Users/...).
$driveLetter = $tempScript.Substring(0, 1).ToLower()
$restOfPath = $tempScript.Substring(2).Replace('\', '/')
$wslScriptPath = "/mnt/host/$driveLetter$restOfPath"

Write-Host "Mounting NAS shares inside the docker-desktop WSL2 distro..."
wsl -d docker-desktop -- sh $wslScriptPath

Remove-Item $tempScript -Force -ErrorAction SilentlyContinue

# The scanhound container's bind-mount sources are only live if the WSL2
# mounts already existed at container-(re)create time. If this script runs
# after the container already started with empty/missing mount sources
# (e.g. right after a host reboot), recreate it now so it picks up the live
# mounts. Harmless no-op if the container already has them.
Write-Host "Recreating the scanhound container to pick up live mounts..."
Push-Location "X:\Docker Apps\ScanHound"
try {
    docker compose up -d
} finally {
    Pop-Location
}

Write-Host "Done."
