<#
.SYNOPSIS
    Dark-deploy the media-kind branch: tag a rollback image, take a VERIFIED
    off-volume backup, deploy, and prove the new code is actually running.

.DESCRIPTION
    "Dark" means the media-kind feature ships switched OFF. No caller passes
    attest_coverage=True, so nothing can be certified and no movie identity can
    exist. Verified against the running container: it emits only `unknown` and
    `tv_season` today, and after this deploy movie identity is still impossible.
    So this adds NO new delete capability -- it only WITHDRAWS identity when two
    listings disagree.

    What it does turn on: the listing-claim ledger (which starts capturing
    evidence that is currently destroyed on every crawl), cross-crawl conflict
    revocation, and the fail-closed authority hold + revocation journal.

    Run with no arguments first. That is read-only and changes nothing.

.PARAMETER Deploy
    Actually tag, back up, rebuild and recreate.

.EXAMPLE
    .\scripts\morning-deploy.ps1            # read-only preflight
    .\scripts\morning-deploy.ps1 -Deploy    # do it
#>
[CmdletBinding()]
param([switch]$Deploy)

$ErrorActionPreference = 'Continue'   # docker writes progress to stderr

$Repo        = 'X:\Docker Apps\ScanHound'
$ComposeFile = 'C:\ProgramData\ScanHound\deploy\docker-compose.yml'
$Project     = 'scanhound'
$Container   = 'scanhound'
$Branch      = 'fix/round12-attestation-authority'
$BackupDir   = 'C:\DockerData\scanhound-backups'
$HealthUrl   = 'http://127.0.0.1:9721/health'

# Strings that exist ONLY on this branch. verify-deploy.py greps the container's
# /app/backend for each. `up -d --build` reuses the tag scanhound:latest, so the
# TAG proves nothing -- only the code inside the container does.
$Markers = @(
    'crawl_attestation_verdict',
    'consume_cross_crawl_conflicts',
    '_revocation_journal_path'
)

function Say($m)  { Write-Host "  $m" }
function Head($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Bad($m)  { Write-Host "  !! $m" -ForegroundColor Red }
function Good($m) { Write-Host "  OK $m" -ForegroundColor Green }

Set-Location -LiteralPath $Repo

# ---------------------------------------------------------------- preflight --
Head "Preflight"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Bad "python is not on PATH. verify-deploy.py cannot run, so a deploy could"
    Bad "not be proven. Open a shell where 'python --version' works."
    exit 2
}

$branch = (git rev-parse --abbrev-ref HEAD)
$head   = (git rev-parse --short HEAD)
$dirty  = (git status --porcelain)

Say "branch     : $branch"
Say "head       : $head"
Say "tree clean : $(if ([string]::IsNullOrWhiteSpace($dirty)) { 'yes' } else { 'NO' })"

if ($branch -ne $Branch) {
    Bad "Expected branch $Branch, found '$branch'. Not switching it for you."
    exit 2
}
if (-not [string]::IsNullOrWhiteSpace($dirty)) {
    Bad "Working tree is dirty. The image is built from the working tree, so an"
    Bad "uncommitted change would ship unreviewed. Files:"
    $dirty -split "`n" | Where-Object { $_ } | ForEach-Object { Say "    $_" }
    exit 2
}
Good "on $Branch, tree clean"

if (-not (Test-Path -LiteralPath $ComposeFile)) {
    Bad "Pinned compose missing: $ComposeFile"
    Bad "That file -- not the repo copy -- created the running container."
    exit 2
}
$pinnedHash = (Get-FileHash -LiteralPath $ComposeFile -Algorithm MD5).Hash
$repoHash   = (Get-FileHash -LiteralPath "$Repo\docker-compose.yml" -Algorithm MD5).Hash
if ($pinnedHash -ne $repoHash) {
    Bad "Pinned compose and repo compose DIFFER. The pinned one is authoritative."
    Say "    compare: git diff --no-index `"$ComposeFile`" `"$Repo\docker-compose.yml`""
    exit 2
}
Good "pinned compose matches the repo copy"

$running = (docker ps --filter "name=$Container" --format '{{.Status}}')
if ([string]::IsNullOrWhiteSpace($running)) { Bad "Container '$Container' not running."; exit 2 }
Say "container  : $running"

$currentImage = (docker inspect $Container --format '{{.Image}}')
Say "image now  : $currentImage"

# --------------------------------------------------------- what will change --
Head "What this deploy changes"
Say "Deploying brings main's media-kind work AND this branch's safety work."
Say ""
Say "Feature state after deploy: DARK."
Say "  Nothing passes attest_coverage=True, so no release can be certified and"
Say "  no movie identity can exist. Verified: the container running right now"
Say "  emits only 'unknown' and 'tv_season' -- there is no movie identity today"
Say "  either. So this deploy adds NO new delete capability."
Say ""
Say "It only WITHDRAWS: TV identity is now revoked when two listings disagree."
Say ""
Say "Newly active: the listing-claim ledger, cross-crawl revocation, and the"
Say "fail-closed authority hold + revocation journal."

if (-not $Deploy) {
    Head "READ-ONLY PREFLIGHT COMPLETE -- nothing changed"
    Say "No backup taken, no image tagged, nothing deployed."
    Say "When you are ready:"
    Say "    .\scripts\morning-deploy.ps1 -Deploy"
    exit 0
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

# ------------------------------------------------------ preserve a rollback --
Head "Tagging the current image so rollback stays possible"
# `up -d --build` REASSIGNS scanhound:latest. Without this, the only name for
# today's known-good image is gone and a routine `docker image prune` deletes
# it permanently.
$rollbackTag = "scanhound:rollback-$stamp"
docker tag scanhound:latest $rollbackTag
$check = (docker image inspect $rollbackTag --format '{{.Id}}' 2>$null)
if ([string]::IsNullOrWhiteSpace($check)) {
    Bad "Could not tag the current image. Refusing to deploy without a rollback."
    exit 1
}
Good "rollback image tagged: $rollbackTag"
Say "    ($check)"

# ------------------------------------------------------------------ backup --
Head "Backing up the database, VERIFIED, off the volume"
# sqlite3's backup API, not a file copy: the DB is in WAL mode, so copying
# crawler.db alone silently omits whatever is still in the -wal file.
# The copy is then written to the HOST, not into scanhound_db -- a backup that
# lives in the same volume as the database shares its failure domain.
$tmpInContainer = "/tmp/crawler-$stamp.db"
$py = @"
import sqlite3, os, sys
src = sqlite3.connect('file:/dbvol/crawler.db?mode=ro', uri=True, timeout=30)
dst = sqlite3.connect('$tmpInContainer')
src.backup(dst)
dst.close()
chk = sqlite3.connect('file:${tmpInContainer}?mode=ro', uri=True)
ok = chk.execute('PRAGMA integrity_check').fetchone()[0]
counts = {}
for t in ('downloads', 'background_scan_cache'):
    counts[t] = chk.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
chk.close()
srcc = {}
for t in ('downloads', 'background_scan_cache'):
    srcc[t] = src.execute('SELECT COUNT(*) FROM %s' % t).fetchone()[0]
src.close()
if ok != 'ok':
    print('INTEGRITY_FAIL ' + str(ok)); sys.exit(3)
if counts != srcc:
    print('COUNT_MISMATCH %s vs %s' % (counts, srcc)); sys.exit(4)
print('VERIFIED size=%d downloads=%d cache=%d' % (
    os.path.getsize('$tmpInContainer'), counts['downloads'], counts['background_scan_cache']))
"@
$backupResult = docker exec $Container python -c $py
$backupExit = $LASTEXITCODE
Say "$backupResult"
if ($backupExit -ne 0 -or $backupResult -notmatch 'VERIFIED') {
    Bad "Backup failed verification (exit $backupExit). NOT deploying."
    exit 1
}

if (-not (Test-Path -LiteralPath $BackupDir)) { New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null }
$hostBackup = Join-Path $BackupDir "crawler-$stamp.db"
docker cp "${Container}:$tmpInContainer" $hostBackup
if (-not (Test-Path -LiteralPath $hostBackup)) {
    Bad "Copy to the host failed. NOT deploying."
    exit 1
}
docker exec $Container rm -f $tmpInContainer | Out-Null
$mb = [math]::Round((Get-Item -LiteralPath $hostBackup).Length / 1MB, 1)
Good "backup on the HOST: $hostBackup ($mb MB), integrity_check ok, row counts match"

# ----------------------------------------------------------------- deploy ---
Head "Building and recreating (10+ minutes -- do not interrupt)"
# --force-recreate: without it, a rebuild that produces an identical image
# leaves the container untouched, and verify-deploy.py correctly reports
# "same image -- nothing was deployed", which reads as a failure on a re-run.
docker compose -f $ComposeFile --project-directory $Repo -p $Project up -d --build --force-recreate
$composeExit = $LASTEXITCODE
if ($composeExit -ne 0) {
    Bad "docker compose exited $composeExit."
    Bad "On PowerShell 5.1 docker's stderr can look like failure on success --"
    Bad "check the container before assuming the worst."
}

# ------------------------------------------------- wait for it to be READY --
Head "Waiting for the app to finish starting"
# Measured startup on this container is ~65s, and the entrypoint's browser-lock
# cleanup has been seen taking 4 minutes. A fixed sleep prints tracebacks from
# post-checks that ran too early and still exits green.
$deadline = (Get-Date).AddSeconds(300)
$ready = $false
while ((Get-Date) -lt $deadline) {
    $h = (docker exec $Container wget -qO- $HealthUrl 2>$null)
    if (-not [string]::IsNullOrWhiteSpace($h)) { $ready = $true; break }
    Start-Sleep -Seconds 5
}
if (-not $ready) {
    Bad "App did not answer /health within 5 minutes."
    Say "    docker compose -f `"$ComposeFile`" -p $Project logs --tail 60 $Container"
    Bad "Do NOT assume the deploy is fine. See rollback at the end of this output."
    exit 1
}
Good "app is up and answering /health"

# ----------------------------------------------------------------- verify ---
Head "Proving the new code is what is running"
$markerArgs = @()
foreach ($m in $Markers) { $markerArgs += '--expect-marker'; $markerArgs += $m }
$LASTEXITCODE = 0
python "$Repo\scripts\verify-deploy.py" after @markerArgs
$verifyExit = $LASTEXITCODE

if ($verifyExit -ne 0) {
    Bad "VERIFY FAILED -- the running container is not the code you think it is."
    Head "ROLLBACK"
    Say "Roll back to the IMAGE, not to git. Do NOT check out main: main has the"
    Say "media-kind feature WITHOUT this branch's safety work, which is a"
    Say "combination that has never been deployed and is less safe than either."
    Say ""
    Say "    docker tag $rollbackTag scanhound:latest"
    Say "    docker compose -f `"$ComposeFile`" --project-directory `"$Repo`" -p $Project up -d --force-recreate"
    exit 1
}
Good "the expected code is running"

# ------------------------------------------------------------ post-checks ---
# All READ-ONLY. Never construct DatabaseManager against the live DB: its
# __init__ runs init_db(), which on a DatabaseError renames crawler.db aside and
# builds an empty one in its place.
Head "Post-deploy sanity (read-only)"

Say "-- the new ledger table exists and is empty --"
docker exec $Container python -c "import sqlite3;c=sqlite3.connect('file:/dbvol/crawler.db?mode=ro',uri=True);print('listing_claims rows:', c.execute('SELECT COUNT(*) FROM listing_claims').fetchone()[0]);c.close()"

Say "-- media kinds recorded (expect none or only pre-existing) --"
    docker exec $Container python -c "import sqlite3;c=sqlite3.connect('file:/dbvol/crawler.db?mode=ro',uri=True);print(c.execute('SELECT media_kind, COUNT(*) FROM downloads GROUP BY media_kind').fetchall());c.close()"

Say "-- attestation is DARK (must print 0) --"
docker exec $Container sh -c "grep -r 'attest_coverage=True' /app/backend/ 2>/dev/null | wc -l"

Head "Done -- deployed dark"
Say "Nothing can certify a media kind, so no new destructive authority exists."
Say "The ledger starts collecting on the next crawl cycle."
Say ""
Say "Rollback image kept: $rollbackTag"
Say "Host backup:         $hostBackup"
Say ""
Say "If you ever need to RESTORE that backup, the whole sequence matters --"
Say "dropping a .db beside a stale -wal corrupts it:"
Say "    docker compose -f `"$ComposeFile`" -p $Project stop $Container"
Say "    docker cp `"$hostBackup`" ${Container}:/dbvol/crawler.db"
Say "    docker run --rm -v ${Project}_scanhound_db:/dbvol alpine sh -c 'rm -f /dbvol/crawler.db-wal /dbvol/crawler.db-shm'"
Say "    docker compose -f `"$ComposeFile`" -p $Project start $Container"
Say ""
Say "Tell Claude it is deployed and it will watch the first cycles from here."
