<#
.SYNOPSIS
    Gathers evidence for the recurring "downloads stop reaching JDownloader"
    fault. Read-only: it changes nothing.

.DESCRIPTION
    ScanHound reaches JDownloader ONLY through MyJDownloader's cloud API at
    api.jdownloader.org. Over 2026-08-21/22 the scanner log recorded 11
    outages in 27 hours, five of them [Errno 101] Network is unreachable --
    the container losing its route out entirely. No application-level timeout
    helps a request that cannot leave the host, so the question is what is
    resetting the network underneath Docker.

    This script correlates those outage timestamps against Windows' own record
    of adapter, Hyper-V switch and WSL events.

.NOTES
    Run ELEVATED. Output is meant to be pasted back verbatim.
#>
[CmdletBinding()]
param(
    [int] $HoursBack = 48,
    [string] $OutFile
)

$ErrorActionPreference = 'Continue'
$since = (Get-Date).AddHours(-$HoursBack)

# The outages ScanHound recorded, as UTC. Correlate against these.
$outages = @(
    '2026-08-21T11:21:16  Network is unreachable'
    '2026-08-21T11:39:40  Network is unreachable'
    '2026-08-21T19:48:55  Network is unreachable'
    '2026-08-21T21:15:44  Network is unreachable'
    '2026-08-22T08:42:04  Network is unreachable'
    '2026-08-21T08:53:10  Read timed out'
    '2026-08-21T11:44:50  Read timed out'
    '2026-08-21T13:22:53  Read timed out'
    '2026-08-21T21:59:09  Read timed out'
    '2026-08-22T11:53:58  Read timed out'
)

function Section($title) {
    Write-Output ''
    Write-Output ('=' * 70)
    Write-Output "  $title"
    Write-Output ('=' * 70)
}

$report = @()

function Emit { param($lines) ; $script:report += $lines ; $lines | Write-Output }

Emit (Section 'SCANHOUND OUTAGES (last success before each failure, UTC)')
Emit $outages

Emit (Section 'ELEVATION')
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Emit "  Elevated: $isAdmin"
if (-not $isAdmin) {
    Emit '  WARNING: some event logs are unreadable without elevation.'
}

Emit (Section 'NETWORK ADAPTERS')
try {
    Get-NetAdapter | Sort-Object Name |
        Format-Table Name, InterfaceDescription, Status, LinkSpeed -AutoSize |
        Out-String -Width 200 | ForEach-Object { Emit $_ }
} catch { Emit "  Get-NetAdapter failed: $_" }

Emit (Section 'HYPER-V VIRTUAL SWITCHES (WSL/Docker ride on these)')
try {
    Get-VMSwitch -ErrorAction Stop |
        Format-Table Name, SwitchType, NetAdapterInterfaceDescription -AutoSize |
        Out-String -Width 200 | ForEach-Object { Emit $_ }
} catch { Emit "  Get-VMSwitch unavailable: $($_.Exception.Message)" }

Emit (Section "ADAPTER / SWITCH EVENTS SINCE $($since.ToString('yyyy-MM-dd HH:mm'))")
$logs = @(
    'Microsoft-Windows-Hyper-V-VmSwitch/Operational'
    'Microsoft-Windows-NetworkProfile/Operational'
    'Microsoft-Windows-Dhcp-Client/Admin'
    'Microsoft-Windows-DNS-Client/Operational'
)
foreach ($log in $logs) {
    Emit ''
    Emit "  --- $log ---"
    try {
        $ev = Get-WinEvent -FilterHashtable @{
            LogName = $log; StartTime = $since
        } -ErrorAction Stop | Where-Object { $_.LevelDisplayName -ne 'Information' }
        if (-not $ev) { Emit '    (no warnings or errors)'; continue }
        $ev | Select-Object -First 25 TimeCreated, Id, LevelDisplayName,
            @{n='Message';e={ ($_.Message -split "`n")[0].Trim() }} |
            Format-Table -AutoSize | Out-String -Width 200 |
            ForEach-Object { Emit $_ }
    } catch {
        Emit "    unavailable: $($_.Exception.Message)"
    }
}

Emit (Section 'WSL / DOCKER SERVICE RESTARTS')
try {
    Get-WinEvent -FilterHashtable @{
        LogName = 'System'; StartTime = $since
    } -ErrorAction Stop |
        Where-Object { $_.Message -match 'wsl|LxssManager|docker|vmcompute|Hyper-V' } |
        Select-Object -First 30 TimeCreated, Id, LevelDisplayName, ProviderName,
            @{n='Message';e={ ($_.Message -split "`n")[0].Trim() }} |
        Format-Table -AutoSize | Out-String -Width 220 |
        ForEach-Object { Emit $_ }
} catch { Emit "  System log unavailable: $($_.Exception.Message)" }

Emit (Section 'CURRENT REACHABILITY FROM THE HOST')
foreach ($t in @('api.jdownloader.org', 'hdencode.org')) {
    try {
        $r = Test-NetConnection -ComputerName $t -Port 443 `
             -InformationLevel Quiet -WarningAction SilentlyContinue
        Emit ("  {0,-24} 443 reachable: {1}" -f $t, $r)
    } catch { Emit "  $t : $($_.Exception.Message)" }
}
Emit ''
Emit '  JDownloader local endpoints:'
foreach ($p in 3128, 3129, 9666) {
    try {
        $r = Test-NetConnection -ComputerName '127.0.0.1' -Port $p `
             -InformationLevel Quiet -WarningAction SilentlyContinue
        $note = switch ($p) {
            3128 { 'MyJD direct connection (myjdapi default)' }
            3129 { 'MyJD direct connection (alt)' }
            9666 { "Click'n'Load -- confirmed reachable from the container" }
        }
        Emit ("  127.0.0.1:{0,-6} open: {1,-6} {2}" -f $p, $r, $note)
    } catch { Emit "  port $p : $($_.Exception.Message)" }
}

Emit (Section 'JDOWNLOADER PROCESS')
try {
    $jd = Get-Process -Name 'JDownloader*', 'java*' -ErrorAction SilentlyContinue
    if ($jd) {
        $jd | Select-Object Id, ProcessName, StartTime,
            @{n='RunningFor';e={ (Get-Date) - $_.StartTime }} |
            Format-Table -AutoSize | Out-String -Width 160 |
            ForEach-Object { Emit $_ }
    } else { Emit '  No JDownloader/java process found.' }
} catch { Emit "  $($_.Exception.Message)" }

Emit (Section 'WHAT TO CHECK BY HAND IN JDOWNLOADER')
Emit @'
  Settings -> Advanced Settings, search: directconnect

    MyJDownloaderSettings: directconnectmode
      Report what this is currently set to.

  Ports 3128 and 3129 both refuse from inside the container, so the direct
  connection listener is not running. If directconnectmode allows a LAN
  listener, enabling it lets ScanHound bypass api.jdownloader.org entirely.

  Also worth reporting:
    Settings -> MyJDownloader -> whether it shows Connected
    Settings -> Advanced Settings, search: clipboard
      (confirm JD is watching the clipboard, and on which account/session)
'@

if ($OutFile) {
    $report | Out-File -FilePath $OutFile -Encoding utf8
    Write-Output ''
    Write-Output "  Written to $OutFile"
}
