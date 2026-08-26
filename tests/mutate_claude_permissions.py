"""Do the tests for scripts/claude-permissions.ps1 actually catch the defects
they were written for?

Passing tests prove nothing on their own. This puts each defect BACK and
requires the relevant test to FAIL. It is the successor to the scratchpad
checker written for round 2 (that one's mutant-1 anchor went stale when the
candidate write moved from WriteAllText to a FileStream); it added the two
mutants for the OPS-7 / SR2-3 round, and now three more for the round-3
defects: D1 (the failure handler asserting unmeasured state), D2 (the ReadOnly
regression) and D3 (the absent allow key).

Round 4 added six more (F1..F6) and, against the charge that this file had
become a RECEIPT for things just fixed rather than a search, two PROBES aimed at
lines nobody in the review sequence had touched. A probe declares its expected
outcome up front and the run is checked against the declaration in BOTH
directions, so a declared survivor is reported as a KNOWN GAP instead of being
quietly absorbed.

ROUND 5 -- THE RECEIPT PROBLEM AGAIN, ONE LEVEL DOWN. Two probes are not a
search. A reviewer picked three lines of the script AT RANDOM, mutated them by
line number, and ALL THREE SURVIVED at 49 passed / 0 failed: the kill rate
outside the lines the findings had named was 0 for 3. Two answers, both here:

  * the three are now dealt with individually. The Test-NoBom length guard is a
    live guard on real input and is now DEFENDED (PROBE C) -- following it to
    its consumer turned up a false claim the read gate was making about any
    file shorter than three bytes. The two post-commit verifications are
    defence in depth and are DECLARED (PROBES D and E), like PROBE B.
  * a RANDOM-LINE mode. It samples executable lines of the script that no
    mutant in this file names, mutates each one BY LINE NUMBER, and reports the
    kill rate as a number. That number does NOT set the exit code: a low kill
    rate is a measurement to publish, not a thing to fix by cherry-picking the
    lines that were sampled.

ROUND 6 -- EVIDENCE THAT LIVES IN A REVIEW IS NOT EVIDENCE. A verifier checked
BY HAND that three round-4/5 guards were real: the V2 discard sentence, the V2
deletion ORDER, and the two V6 controls. Each check was correct and none of it
was in the repository, which is the same provenance problem this file's line
numbers had. Four mutants (V2-a, V2-b, V6-a, V6-b) now carry it here.

That changes the RANDOM-LINE POOL, and the change is disclosed rather than
absorbed: named_by_a_finding() excludes every script line any mutant names, so
adding four mutants removes their anchor lines from the pool. The round-5 figure
"7 of 12 on seed 20260826" therefore does NOT reproduce on this revision, and a
comparison across the two revisions is not like for like. The number is a
measurement of a moving target and is published as it comes, not tracked as a
score.

AND NO LINE NUMBER IS WRITTEN DOWN ANY MORE. The labels used to say "line 663"
and "line 714". Measured, at the commit those labels shipped in:

    git show 67ba85e:scripts/claude-permissions.ps1 | sed -n '663p'
        Say "Move-Item -Force: that succeeded here and silently cleared your"
    git show 67ba85e:scripts/claude-permissions.ps1 | sed -n '714p'
        # Measured BEFORE the commit, so the handler below can compare rather than

Neither is a line any probe edits. The anchors were at 675 and 726 in 67ba85e
and at 453 and 504 in its parent 9569159, so an auditor opening 663 found
unrelated code and had every reason to conclude the provenance was invented. The
claim underneath was true at the right numbers; the number was the part nobody
could maintain. Every line number this file prints is now COMPUTED from the
anchor at run time, and provenance is quoted as a git command anchored to the
line's TEXT, which survives edits above it.

Run from the repository root:

    python tests/mutate_claude_permissions.py            # declared + random
    python tests/mutate_claude_permissions.py --no-random
    python tests/mutate_claude_permissions.py --only-random --seed 7 --random-lines 20
"""
import io
import os
import random
import subprocess
import sys

SCRIPT = "scripts/claude-permissions.ps1"
TESTS = "tests/test_claude_permissions_script.ps1"

ORIG = {p: io.open(p, encoding="utf-8", newline="").read() for p in (SCRIPT, TESTS)}

KILL = "KILL"
SURVIVE_BY_DESIGN = "SURVIVE_BY_DESIGN"
# A mutant aimed at a line NOBODY in the review sequence has touched. The point
# is to stop this file being a receipt for things just fixed. The declared
# outcome is what the run is checked against, and PROBE_SURVIVE is a declared
# GAP: the suite is asserted to be completely blind to the edit, and the note
# has to say why that is or is not acceptable. A probe whose declaration turns
# out to be wrong fails the run, in either direction.
PROBE_KILL = "PROBE_KILL"
PROBE_SURVIVE = "PROBE_SURVIVE"

# --------------------------------------------------------------- edits ------

BOM_NEW = """    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes($json)
    $fs = New-Object System.IO.FileStream($tmp,
            [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None)
    try {
        $fs.Write($bytes, 0, $bytes.Length)
        $fs.Flush($true)
    } finally { $fs.Dispose() }
"""
BOM_OLD = """    $json | Set-Content -Path $tmp -Encoding UTF8
"""

REVOKE_NEW = """    $wanted   = @($existing | Where-Object { $_ -isnot [string] -or $OWNED -notcontains $_ })
    $removing = @($existing | Where-Object { $_ -is [string] -and $OWNED -contains $_ })"""
REVOKE_OLD = """    $scoped   = if ($IncludeDeploy) { $MERGE_RULES + $DEPLOY_RULES } else { $MERGE_RULES }
    $wanted   = @($existing | Where-Object { $_ -isnot [string] -or $scoped -notcontains $_ })
    $removing = @($existing | Where-Object { $_ -is [string] -and $scoped -contains $_ })"""

# Swaps ONLY the commit primitive. The surrounding try/catch stays, so the
# mutant isolates File.Replace-vs-Move-Item and nothing else.
#
# ANCHOR MOVED 2026-08-26: the third argument was [NullString]::Value until the
# D1 fix gave ReplaceFile a real lpBackupFileName. The checker reported this as
# "ANCHOR MATCHED 0 TIMES -- skipped, proves nothing" rather than quietly
# passing, which is the only reason it is not still stale.
COMMIT_NEW = """    [System.IO.File]::Replace($candidate, $SettingsPath, $replacedCopy)
"""
COMMIT_OLD = """    Move-Item -Path $candidate -Destination $SettingsPath -Force
"""

EXIT_NEW = """Say "reloaded it -- restart Claude Code for this to take effect."

# Explicit, so "success means exit 0" is a contract the tests can assert rather
# than an accident of whatever the last statement happened to leave behind
# (SR2-3).
exit 0
"""
# State is written correctly and verified; only the process contract is broken.
EXIT_BROKEN = """Say "reloaded it -- restart Claude Code for this to take effect."

exit 3
"""

HARNESS_NEW = """    return [pscustomobject]@{ Output = ($out + $errTask.Result); ExitCode = $code }
"""
HARNESS_OLD = """    return [pscustomobject]@{ Output = ($out + $errTask.Result); ExitCode = 0 }
"""

# -------- round 3: the three defects the OPS-7 evidence did not reach --------

# D1. The whole pre-fix commit: lpBackupFileName NULL, and a catch that asserts
# what it never measured and then deletes the candidate.
D1_NEW = """try {
    [System.IO.File]::Replace($candidate, $SettingsPath, $replacedCopy)
} catch {
    Write-CommitFailureReport -Err "$_" -Destination $SettingsPath -Candidate $candidate -ReplacedCopy $replacedCopy -Backup $backup -PreCommitHash $preCommitHash
}
"""
D1_OLD = """try {
    [System.IO.File]::Replace($candidate, $SettingsPath, [NullString]::Value)
} catch {
    Remove-Item $candidate -Force -ErrorAction SilentlyContinue
    Die "the commit FAILED; settings.json is UNCHANGED and the candidate was discarded. Backup remains at $backup. $_"
}
"""

# D2. Drop the read-only refusal. The script then reaches ReplaceFile, which
# fails with a bare "Access to the path is denied." -- after a backup has
# already been written. Fail-closed either way; the difference is whether the
# operator is told the cause, given the fix, and spared the litter.
D2_NEW = """if ($destReadOnly) {
    Write-Host ""
    Say "This is a CHANGE from the pre-2026-08-26 script, which committed with"
    Say "Move-Item -Force: that succeeded here and silently cleared your"
    Say "READ-ONLY attribute as a side effect. This script will not strip a"
    Say "protection you set; clear it yourself if you meant to."
    Write-Host ""
    Die ("$SettingsPath carries the READ-ONLY attribute. The commit primitive " +
         "(ReplaceFile) refuses a read-only destination with 'Access to the path " +
         "is denied.' Nothing was written and no backup was taken. Clear the " +
         "attribute and re-run:  attrib -R " + [char]34 + $SettingsPath + [char]34)
}
"""
D2_OLD = """# (read-only refusal removed by the mutation checker)
"""

# D3. Drop the allow-key normalisation, restoring @($null).Count -eq 1 and the
# SetValueInvocationException thrown outside any try/catch.
D3_NEW = """$allowProp = $settings.permissions.PSObject.Properties['allow']
if ($null -eq $allowProp) {
    $settings.permissions | Add-Member -MemberType NoteProperty -Name 'allow' -Value @()
} elseif ($null -eq $allowProp.Value) {
    $settings.permissions.allow = @()
}
"""
D3_OLD = """# (allow-key normalisation removed by the mutation checker)
"""

# -------- round 4: the six findings the round-3 evidence did not reach -------

# F1. Collapse readability back into parseability -- the round-3 shape. Any
# exception out of ReadAllText, a sharing violation included, becomes
# "does not parse", which the branch prose then states as fact about content.
F1_NEW = """        $text = $null
        # Two separate try blocks ON PURPOSE. Merging them is exactly the F1
        # defect: it makes "the OS would not let me open this" indistinguishable
        # from "I read it and it is not JSON".
        try   { $text = [System.IO.File]::ReadAllText($Destination); $destReadable = $true }
        catch { $destReadable = $false }
        if ($destReadable) {
            try   { $null = ($text -replace "^\\xEF\\xBB\\xBF", '' | ConvertFrom-Json); $destParses = $true }
            catch { $destParses = $false }
        }
"""
F1_OLD = """        $destReadable = $true
        try {
            $null = ([System.IO.File]::ReadAllText($Destination) -replace "^\\xEF\\xBB\\xBF", '' | ConvertFrom-Json)
            $destParses = $true
        } catch { $destParses = $false }
"""

# F3. Put the unmeasured row back under the MEASURED heading.
F3_NEW = """    Say ("    timestamped backup ......... {0}" -f $(if ($bakExists) { $Backup } else { 'NOT PRESENT' }))
"""
F3_OLD = """    Say ("    timestamped backup ......... {0}" -f $Backup)
"""

# F4. Remove the allow TYPE guard, leaving only D3's absent/null normalisation.
# The two REFUSALS only, disabled one at a time in a single mutant.
# $allowIsArray and $badIndexes are left in place, so the counting and the
# revoke path still work: the mutant isolates "does it refuse" from "does it
# know the shape".
F4A_NEW = """if (-not $allowIsArray) {
"""
F4A_OLD = """if ($false) {   # (not-array refusal removed by the mutation checker)
"""
F4B_NEW = """if ($badIndexes.Count -gt 0 -and -not $Revoke) {
"""
F4B_OLD = """if ($false) {   # (non-string refusal removed by the mutation checker)
"""

# V5a. The refusal gates the UNDO again -- F4's shape, one guard for both
# directions. Behaviour on every well-formed input is identical, which is why
# no case in the suite could see it before the revoke-with-a-null case existed.
V5A_NEW = """if ($badIndexes.Count -gt 0 -and -not $Revoke) {
"""
V5A_OLD = """if ($badIndexes.Count -gt 0) {
"""

# V5b. -WhatIf goes back to exiting nonzero over a shape it would refuse,
# against .PARAMETER WhatIf ("Show the change; write nothing") and the suite's
# own "-WhatIf writes nothing AND reports success" contract.
V5B_NEW = """    if ($WhatIf) {
        Warn ("-WhatIf: a real run would REFUSE. " + $bad)
        Warn "-WhatIf: nothing written"
        exit 0
    }
    Die $bad
"""
V5B_OLD = """    Die $bad
"""

# F5. The privilege escalation: a plain grant silently hands over the three
# container-lifecycle rules, and the paragraph explaining them is skipped
# because $IncludeDeploy is still $false.
F5_NEW = """    $grant    = if ($IncludeDeploy) { $MERGE_RULES + $DEPLOY_RULES } else { $MERGE_RULES }
"""
F5_OLD = """    $grant    = $MERGE_RULES + $DEPLOY_RULES
"""

# F6. Restore the sentence that is false whenever a rule string was already in
# the file. Behaviour is IDENTICAL -- only the claim changes.
F6_NEW = """    Write-Host "  It removes these rule STRINGS from the allow list whether or not" -ForegroundColor Yellow
    Write-Host "  this script is what added them: matching is by exact rule text" -ForegroundColor Yellow
    Write-Host "  against the fixed set of rules this script can grant, and nothing" -ForegroundColor Yellow
    Write-Host "  anywhere records who added a rule. If you typed one of the lines" -ForegroundColor Yellow
    Write-Host "  listed above yourself, it goes too -- check the list before" -ForegroundColor Yellow
    Write-Host "  continuing." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  It does NOT revoke capability granted by other allow rules --" -ForegroundColor Yellow
"""
F6_OLD = """    Write-Host "  This removes only the entries THIS SCRIPT owns. It does not" -ForegroundColor Yellow
    Write-Host "  It does NOT revoke capability granted by other allow rules --" -ForegroundColor Yellow
"""

# F2/V1. Grow the handler a seventh verdict ARM that no test drives AND that
# declares nothing -- a real, reachable branch of exactly the shape a future
# edit would add. Reachable whenever the destination is present, readable,
# valid, non-identical and a ReplaceFile backup copy exists.
#
# THIS IS THE MUTANT THE ROUND-4 VERSION COULD NOT KILL. Its F2 added
# "# BRANCH:hypothetical" by hand, so the coverage case saw seven markers for
# six modes and failed -- on the marker, never on the branch. Measured against
# the round-4 suite, the arm below left it at 49 passed / 0 failed.
F2_NEW = """    } else {
        # BRANCH:altered
"""
F2_OLD = """    } elseif ($keptCopy) {
        Warn "settings.json EXISTS and parses, and a ReplaceFile backup copy is present."
        Say ("recover with:  Move-Item -LiteralPath '" + $ReplacedCopy + "' -Destination '" + $Destination + "'")
        $verdict = ("the commit FAILED PARTWAY: a ReplaceFile backup copy of the destination is " +
                    "present. Nothing was deleted; recover using the paths listed above.")

    } else {
        # BRANCH:altered
"""

# F2d. The round-4 mutant, kept: a marker that names no arm. It exercises a
# different rule of the same case -- markers found inside verdict arms must be
# exactly the markers found anywhere in the script.
F2D_NEW = """        # BRANCH:altered
"""
F2D_OLD = """        # BRANCH:altered
        # BRANCH:hypothetical
"""

# F2a. The unparseable branch stops distinguishing "I read it and it is not
# JSON" from the round-3 wording that covered both cases at once.
F2A_NEW = """        Warn "settings.json EXISTS, was READ, and does NOT parse as JSON."
"""
F2A_OLD = """        Warn "settings.json EXISTS but does not parse."
"""

# F2b. The altered branch goes back to printing only "compare against", which
# is what it did while the verdict beneath it said to recover using the paths
# listed above and the commit message claimed a recovery command.
F2B_NEW = """            Say ("compare against:  " + $Backup)
            Say ("recover with:  " + $restoreCmd)
"""
F2B_OLD = """            Say ("compare against:  " + $Backup)
"""

# F2c. Delete the identity-unknown branch, so a readable, valid destination
# whose pre-commit hash was never taken is reported as ALTERED.
F2C_NEW = """    } elseif (-not $identityKnown) {
        # BRANCH:identity-unknown
        # Readable and valid, but a hash was unavailable on one side, so
        # "differs" is not a thing this run may say. Same rule as the
        # unreadable branch: report the gap, recommend no overwrite.
        Warn "settings.json EXISTS and parses, but whether it still matches its"
        Warn "pre-commit bytes was NOT DETERMINED -- one of the two hashes could not be taken."
        if ($bakExists) { Say ("compare against:  " + $Backup) }
        $verdict = ("the commit FAILED and the destination's identity was NOT determined: " +
                    "settings.json parses, but this run cannot say whether it changed. " +
                    "Nothing was deleted; compare against the paths listed above before overwriting anything.")

    } else {
"""
F2C_OLD = """    } else {
"""

# -------- probes: lines NOBODY has touched in this review sequence -----------
# Every probe target is unmodified since 8ae7837, the commit that created the
# file, and none was named by OPS-1..7, SR2-1..3, D1..D3 or F1..F6. Each note
# carries the command that shows it, anchored to the line's TEXT rather than to
# its number so that it keeps working when lines move.

# PROBE A. The backup becomes an EMPTY file. It still exists, it is still
# timestamped, and every "recover with: Copy-Item" the failure handler prints
# still names it.
PROBE_BACKUP_NEW = """Copy-Item $SettingsPath $backup
"""
PROBE_BACKUP_OLD = """New-Item -ItemType File -Path $backup | Out-Null
"""

# PROBE B. Delete the post-commit BOM verification of the LIVE file.
PROBE_VERIFY_NEW = """if (-not (Test-NoBom $SettingsPath)) { Die "the live file has a BOM after commit. Restore from $backup" }
"""
PROBE_VERIFY_OLD = """# (post-commit BOM verification removed by the mutation checker)
"""

# PROBE C. Test-NoBom's length guard: a file shorter than three bytes now PASSES
# the check instead of failing it.
PROBE_SHORT_NEW = """    if ($n -lt 3) { return $false }          # too short to be our settings file
"""
PROBE_SHORT_OLD = """    if ($n -lt 3) { return $true }           # (length guard inverted by the mutation checker)
"""

# PROBE D. Delete the post-commit verification that every rule being ADDED is
# actually in the live file.
PROBE_ADDED_NEW = """foreach ($r in $adding)   { if ($live -notcontains $r) { Die "live file is missing $r. Restore from $backup" } }
"""
PROBE_ADDED_OLD = """# (post-commit added-rule verification removed by the mutation checker)
"""

# PROBE E. Delete the post-commit verification that no unrelated top-level key
# was lost.
PROBE_KEYS_NEW = """foreach ($k in $otherKeys){ if (-not ($final.PSObject.Properties.Name -contains $k)) { Die "live file lost '$k'. Restore from $backup" } }
"""
PROBE_KEYS_OLD = """# (post-commit key-survival verification removed by the mutation checker)
"""

# ---- round 6 (R5): guards whose only evidence lived in a review, not here ---
#
# R5. Three guards added in rounds 4-5 rested on their test cases alone. A
# reviewer confirmed BY HAND that each is real -- and that hand-check lived in a
# review document, which is exactly the provenance problem the rest of this file
# was rewritten to stop having. The repository now carries its own evidence.

# V2-a. Put back the sentence that stated a discard it never checked: the
# BRANCH:unchanged verdict said "The candidate was discarded." unconditionally,
# with -ErrorAction SilentlyContinue upstream making a failed deletion silent.
V2_MSG_NEW = """             $(if ($candLeft) { "The candidate could NOT be discarded and is STILL AT $Candidate. " }
               else { "The candidate was discarded. " }) +
"""
V2_MSG_OLD = """             "The candidate was discarded. " +
"""

# V2-b. Put the deletions back BELOW the MEASURED table, inside the branch that
# describes the files being deleted. Two edits to the same file: remove the
# block above the table, and re-add it inside BRANCH:unchanged.
V2_ORDER_NEW = """    if ($verifiedIdentical) {
        if (Test-Path -LiteralPath $Candidate)    { Remove-Item $Candidate    -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $ReplacedCopy) { Remove-Item $ReplacedCopy -Force -ErrorAction SilentlyContinue }
    }
"""
V2_ORDER_OLD = """    # (deletions moved back below the table by the mutation checker)
"""
V2_ORDER2_NEW = """        Die ("settings.json is UNCHANGED -- verified byte-identical to the copy hashed " +
"""
V2_ORDER2_OLD = """        if (Test-Path -LiteralPath $Candidate)    { Remove-Item $Candidate    -Force -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $ReplacedCopy) { Remove-Item $ReplacedCopy -Force -ErrorAction SilentlyContinue }
        Die ("settings.json is UNCHANGED -- verified byte-identical to the copy hashed " +
"""

# V6-a. Curate the allow list: drop empty-string entries while granting. This is
# the tidy-up F4 refused -- an entry the operator put there, removed by a script
# that does not own it and never said it would.
V6_EMPTY_NEW = """    $wanted   = @($existing + $adding)
"""
# The inner @() is not decoration. Without it a one-element allow list comes out
# of Where-Object as a SCALAR STRING, and "string + array" is string
# concatenation in PowerShell, so the mutant would introduce a second unrelated
# defect and stop isolating the curation. Measured: it broke the F4 control too.
V6_EMPTY_OLD = """    $wanted   = @(@($existing | Where-Object { $_ -isnot [string] -or $_.Trim() -ne '' }) + $adding)
"""

# V6-b. Revoke only the FIRST copy of each owned rule. A surviving duplicate is
# a standing authorization the operator was told had been revoked.
V6_DUPE_NEW = """    $wanted   = @($existing | Where-Object { $_ -isnot [string] -or $OWNED -notcontains $_ })
    $removing = @($existing | Where-Object { $_ -is [string] -and $OWNED -contains $_ })
"""
V6_DUPE_OLD = """    $wanted   = @()
    $removing = @()
    $seen     = @()
    foreach ($e in $existing) {
        if ($e -is [string] -and $OWNED -contains $e -and $seen -notcontains $e) {
            $removing += $e
            $seen     += $e
        } else {
            $wanted += $e
        }
    }
"""

# ------------------------------------------------------------- mutants ------
# (label, mode, [(file, old, new), ...], must_fail_substrings, note)
MUTANTS = [
    ("BOM: revert the candidate write to Set-Content -Encoding UTF8"
     " (the production incident)",
     KILL,
     [(SCRIPT, BOM_NEW, BOM_OLD)],
     ["grant adds the merge rule"],
     "The expectation is NOT 'the no-BOM test fails'. With prepare->validate\n"
     "  ->commit a BOM-writing bug is caught in the CANDIDATE and the script\n"
     "  refuses to commit, so the live file never gets a BOM and that test\n"
     "  passes trivially. What must fail is the GRANT itself: the rule does not\n"
     "  land, because the script correctly declined to write. Failing closed IS\n"
     "  the design."),

    ("OPS-6: revert revoke to only remove what the CURRENT flags name",
     KILL,
     [(SCRIPT, REVOKE_NEW, REVOKE_OLD)],
     ["PLAIN revoke"],
     "A standing authorization survives an undo the user believes was complete."),

    ("OPS-7: revert the commit from File.Replace to Move-Item -Force",
     KILL,
     [(SCRIPT, COMMIT_NEW, COMMIT_OLD)],
     ["PRESERVES the live file's ACL"],
     "Move-Item moves the CANDIDATE onto the path, so the surviving file keeps\n"
     "  the temp file's inherited ACL and any explicit ACE on settings.json is\n"
     "  silently dropped. The failure-injection commit test is expected to keep\n"
     "  passing: the try/catch around the primitive is untouched, so a locked\n"
     "  destination still fails closed either way. The ACL is the discriminator."),

    ("SR2-3: script writes the right state, then exits 3",
     KILL,
     [(SCRIPT, EXIT_NEW, EXIT_BROKEN)],
     ["a CORRECT grant that exits nonzero is still a failure"],
     "Every on-disk assertion in the suite passes on this build. Only an\n"
     "  exit-code assertion can see it -- the exact gap SR2-3 named."),

    ("SR2-3 CONTROL: the same exit-3 defect, with Invoke-Script blinded to the"
     " child's exit code",
     SURVIVE_BY_DESIGN,
     [(SCRIPT, EXIT_NEW, EXIT_BROKEN), (TESTS, HARNESS_NEW, HARNESS_OLD)],
     ["a CORRECT grant that exits nonzero is still a failure"],
     "This is the control, and it is SUPPOSED to survive. It reconstructs the\n"
     "  pre-fix harness -- one that cannot see the child's exit code -- on top of\n"
     "  the same defect mutant 4 kills. If the named test stops failing here,\n"
     "  the exit-code plumbing in Invoke-Script is what does the killing, not\n"
     "  something incidental. Refusal-path cases DO fail here, for a different\n"
     "  reason: a blinded harness reports 0 for genuine refusals too."),

    ("D1: revert the commit to lpBackupFileName=NULL and an UNVERIFIED"
     " 'settings.json is UNCHANGED'",
     KILL,
     [(SCRIPT, D1_NEW, D1_OLD)],
     ["does not claim UNCHANGED when the destination is GONE"],
     "The LOCKED-destination case cannot discriminate the two handlers -- there\n"
     "  the live file really is unchanged, so the unconditional sentence happens\n"
     "  to be true, and that case keeps passing here. What kills this mutant is\n"
     "  the case where the sentence is FALSE: the destination is deleted the\n"
     "  instant the candidate appears, so ReplaceFile fails with the destination\n"
     "  genuinely absent. The mutant then prints 'settings.json is UNCHANGED'\n"
     "  about a file that is not there, and deletes the candidate -- the only\n"
     "  other copy of the intended content."),

    ("D2: remove the READ-ONLY refusal, so the attribute case reaches"
     " ReplaceFile bare",
     KILL,
     [(SCRIPT, D2_NEW, D2_OLD)],
     ["READ-ONLY destination is refused"],
     "The mutant still fails closed -- File.Replace throws 'Access to the path\n"
     "  is denied.' and the file is untouched -- so an exit-code-plus-bytes\n"
     "  assertion alone would NOT catch it. The regression is the operator\n"
     "  experience: no cause named, no fix offered, and a .bak- copy already\n"
     "  written for a run that could never have committed. Those are what the\n"
     "  test asserts, and they are what this mutant breaks."),

    ("D3: remove the allow-key normalisation (@($null).Count is 1, and the"
     " assignment throws)",
     KILL,
     [(SCRIPT, D3_NEW, D3_OLD)],
     ["FRESH-USER shape", "NO allow key counts 0 rules"],
     "The fresh-user shape -- a deny list and no allow key -- is the input every\n"
     "  one of the previous 23 assertions was structurally unable to see, because\n"
     "  New-Fixture always supplied a three-element allow list. With the factory\n"
     "  able to emit the shape, the defect is visible twice over: a confident\n"
     "  wrong count (1 for 0) and a raw SetValueInvocationException thrown\n"
     "  outside any try/catch."),

    # ------------------------------------------------------------ round 4 ---

    ("F1: collapse 'could not READ it' back into 'does not parse'",
     KILL,
     [(SCRIPT, F1_NEW, F1_OLD)],
     ["UNREADABLE destination reports UNKNOWN"],
     "The round-3 REGRESSION, and the reason it was guaranteed rather than an\n"
     "  edge: Get-FileHashHex (OpenRead, FileShare.Read) and ReadAllText (also\n"
     "  FileShare.Read) fail TOGETHER, so an unhashable destination was ALWAYS\n"
     "  also an 'unparseable' one. Every run that honestly printed 'byte-identical\n"
     "  ... UNKNOWN' was then contradicted two lines down by 'EXISTS but does not\n"
     "  parse' and 'is NOT in its pre-commit state' -- about a file that was, at\n"
     "  that instant, byte-identical and valid. The locked-with-FileShare::Read\n"
     "  case cannot see this (there the hash succeeds); FileShare::None can."),

    ("F2/V1: give the handler a seventh verdict ARM, carrying NO marker",
     KILL,
     [(SCRIPT, F2_NEW, F2_OLD)],
     ["every verdict ARM of the handler"],
     "THE MUTANT THE ROUND-4 SUITE COULD NOT KILL, and the reason this file's\n"
     "  F2 was a receipt: round 4's version added '# BRANCH:hypothetical' BY HAND,\n"
     "  so the coverage case failed on a marker count, never on a branch. A real\n"
     "  seventh arm carrying no marker left that suite at 49 passed / 0 failed.\n"
     "  The coverage case now reads the arms out of the PARSE TREE, so an arm\n"
     "  counts because it exists and reaches a verdict -- not because somebody\n"
     "  remembered to describe it. If this survives, the axis is decoration."),

    ("F2d: a marker that names no arm (round 4's version of F2, kept)",
     KILL,
     [(SCRIPT, F2D_NEW, F2D_OLD)],
     ["every verdict ARM of the handler"],
     "The other direction of the same rule: markers found INSIDE verdict arms\n"
     "  must be exactly the markers found anywhere in the script, so a stray\n"
     "  marker cannot pad the set and make an uncovered arm look covered."),

    ("F2a: the unparseable branch stops saying the file was READ",
     KILL,
     [(SCRIPT, F2A_NEW, F2A_OLD)],
     ["handler branch 'unparseable'"],
     "The branch that had NO assertion and NO mutant before this round. Its\n"
     "  whole job now is to be the CONTENT verdict -- reached only after the file\n"
     "  was actually read -- so the wording that covers reading and not-reading at\n"
     "  once is the defect, not a phrasing preference."),

    ("F2b: the altered branch prints 'compare against' and no recovery command",
     KILL,
     [(SCRIPT, F2B_NEW, F2B_OLD)],
     ["handler branch 'altered'"],
     "The other branch with no assertion and no mutant. Its verdict line tells\n"
     "  the operator to 'recover using the paths listed above' and the round-3\n"
     "  commit message claimed the non-identical branches print 'a recovery\n"
     "  command' -- while this one printed a path to diff against and nothing to\n"
     "  run."),

    ("F2c: delete the identity-unknown branch, so 'no hash' is reported as ALTERED",
     KILL,
     [(SCRIPT, F2C_NEW, F2C_OLD)],
     ["handler branch 'identity-unknown'"],
     "F1 pointing at the other verdict. An absent pre-commit hash is not\n"
     "  evidence the file changed, exactly as an access error is not evidence it\n"
     "  is invalid; without this branch the handler prints the honest 'NOT\n"
     "  DETERMINED' row and then states 'is NOT byte-identical' underneath it.\n"
     "  The coverage case fails here too, and should: the marker is gone while\n"
     "  the axis still names the mode."),

    ("F3: print the timestamped-backup row unmeasured, under a MEASURED heading",
     KILL,
     [(SCRIPT, F3_NEW, F3_OLD)],
     ["'timestamped backup' row is MEASURED"],
     "The two rows immediately above it are Test-Path-guarded and print\n"
     "  'not created'/'no'. This one printed $Backup whether or not the file was\n"
     "  there -- and both recovery commands the handler emits are built from that\n"
     "  path, so the row being unmeasured means handing the operator a Copy-Item\n"
     "  from a file that does not exist."),

    ("F4: remove the allow TYPE guard, leaving only D3's absent/null handling",
     KILL,
     [(SCRIPT, F4A_NEW, F4A_OLD), (SCRIPT, F4B_NEW, F4B_OLD)],
     ["JSON OBJECT is refused", "bare STRING is refused", "non-string is refused"],
     "D3 closed ABSENT and NULL. Three more shapes walked through the same door:\n"
     "  an object counted as '1 rule(s)' and COMMITTED into the allow list, a\n"
     "  scalar silently rewritten into a list, and a null counted as a rule and\n"
     "  preserved. Same key, same confident wrong number D3 named, plus a\n"
     "  malformed allow list written into a security file."),

    ("F5: a PLAIN grant silently hands over the three deploy rules",
     KILL,
     [(SCRIPT, F5_NEW, F5_OLD)],
     ["PLAIN grant does NOT add the deploy rules"],
     "The suite covered the positive direction only -- that -IncludeDeploy DOES\n"
     "  produce Bash(docker restart:*). Honest note on the finding as filed: it\n"
     "  claimed this mutant left the suite at 0 failed. Measured, it did NOT --\n"
     "  the fresh-user allow-key case caught it INCIDENTALLY with 'expected\n"
     "  exactly 1 rule, got 4'. That is an accidental kill by a case written for\n"
     "  an unrelated axis, which would vanish the moment that count assertion was\n"
     "  reworded. The named assertion is what this mutant is now checked against."),

    ("F6: restore 'This removes only the entries THIS SCRIPT owns'",
     KILL,
     [(SCRIPT, F6_NEW, F6_OLD)],
     ["removes OWNED rule strings a HUMAN added"],
     "BEHAVIOUR IS IDENTICAL under this mutant; only the claim changes. That is\n"
     "  the point -- no state assertion anywhere can catch it, and no test could\n"
     "  even set up the scenario until New-Fixture -Shape PreOwned let a fixture\n"
     "  start with an owned rule string a human 'typed by hand'."),

    # ------------------------------------------------------------ round 5 ---

    ("V5a: the allow-shape refusal gates the UNDO again (F4's shape)",
     KILL,
     [(SCRIPT, V5A_NEW, V5A_OLD)],
     ["-Revoke over an allow list with a NULL entry"],
     "Behaviour on every well-formed input is IDENTICAL under this mutant, which\n"
     "  is why nothing in the round-4 suite could see it: one malformed entry\n"
     "  anywhere in the allow list and the operator cannot revoke gh pr merge\n"
     "  without a hand edit. The same file argues that a revoke leaving a\n"
     "  standing authorization in place is the worst outcome available, so this\n"
     "  is that outcome arriving from the other direction."),

    ("V5b: -WhatIf exits nonzero over a shape a real run would refuse",
     KILL,
     [(SCRIPT, V5B_NEW, V5B_OLD)],
     ["-WhatIf over a malformed allow"],
     "Nothing is written either way -- only the process contract changes, and\n"
     "  only for an input no case covered until this round. .PARAMETER WhatIf\n"
     "  says 'Show the change; write nothing' and the suite's stated contract is\n"
     "  'no-op / WhatIf exit 0 AND the bytes are byte-identical'."),

    # -------------------------------------------------- untouched-line probes
    ("PROBE A: the backup becomes an EMPTY file",
     PROBE_KILL,
     [(SCRIPT, PROBE_BACKUP_NEW, PROBE_BACKUP_OLD)],
     ["HOLDS THE PRE-CHANGE BYTES"],
     "Aimed at a line no finding in this sequence named. Until the F5 sweep the\n"
     "  backup case asserted only that a .bak-* file EXISTS, so an empty backup\n"
     "  passed -- while every 'recover with: Copy-Item' the failure handler\n"
     "  prints names exactly that file. Declared expectation: KILLED by the byte\n"
     "  comparison added in round 4.\n"
     "  Provenance, anchored to the TEXT so it survives edits above it:\n"
     "    git log -L '/^Copy-Item $SettingsPath $backup$/,+1:scripts/claude-permissions.ps1'"),

    ("PROBE B: delete the post-commit BOM check on the LIVE file",
     PROBE_SURVIVE,
     [(SCRIPT, PROBE_VERIFY_NEW, PROBE_VERIFY_OLD)],
     [],
     "Declared expectation: SURVIVES, and this is reported as a KNOWN GAP rather\n"
     "  than dressed up as a control. The line is defence in depth: the candidate\n"
     "  is already proven BOM-free before the commit, and File.Replace moves\n"
     "  those exact bytes, so on any build where the candidate validator works\n"
     "  this check can never fire and deleting it changes nothing observable.\n"
     "  A test could only kill it by ALSO breaking the validator, i.e. by\n"
     "  testing two defects at once. It is kept because the day the validator\n"
     "  IS wrong is the day it matters -- but no assertion in this suite defends\n"
     "  it, and pretending otherwise is what this file exists to prevent.\n"
     "  Provenance:\n"
     "    git log -L '/^if (-not (Test-NoBom $SettingsPath)) { Die/,+1:scripts/claude-permissions.ps1'\n"
     "  Its message shares Test-NoBom's two meanings -- it says 'has a BOM' for a\n"
     "  file that may merely be short -- and is deliberately NOT reworded, so\n"
     "  this probe's provenance claim stays true. The reachable twin of that\n"
     "  conflation, at the READ gate, is fixed and defended by PROBE C."),

    # ----------------------------------------------- round 5: the three the
    #                                                  reviewer picked at random
    ("PROBE C: Test-NoBom's length guard lets a sub-3-byte file PASS",
     PROBE_KILL,
     [(SCRIPT, PROBE_SHORT_NEW, PROBE_SHORT_OLD)],
     ["SHORTER THAN THREE BYTES"],
     "One of three lines a reviewer picked at random and mutated. All three\n"
     "  survived at 49 passed / 0 failed -- a kill rate of 0 for 3 outside the\n"
     "  lines the findings had named. This one is a live guard on real input, so\n"
     "  the answer is a test rather than a declaration. Following it to its\n"
     "  CONSUMER is what made the test possible: the read gate turned both of\n"
     "  Test-NoBom's meanings into one sentence and told a two-byte file it\n"
     "  'already has a BOM', which is a cause it never observed. Declared\n"
     "  expectation: KILLED, by the case that drives a two-byte fixture.\n"
     "  Provenance:\n"
     "    git log -L '/if ($n -lt 3)/,+1:scripts/claude-permissions.ps1'"),

    ("PROBE D: delete the post-commit check that every ADDED rule is in the live file",
     PROBE_SURVIVE,
     [(SCRIPT, PROBE_ADDED_NEW, PROBE_ADDED_OLD)],
     [],
     "Declared expectation: SURVIVES, reported as a KNOWN GAP. Same shape as\n"
     "  PROBE B: the candidate was already proven to contain every added rule\n"
     "  before the commit, and File.Replace moves those exact bytes, so on any\n"
     "  build where the candidate validator works this check cannot fire. A test\n"
     "  could only kill it by ALSO breaking the validator -- two defects at once,\n"
     "  which measures neither. It is kept for the day the validator is wrong, or\n"
     "  another process writes between the commit and this line. What is NOT\n"
     "  acceptable is pretending the suite defends it.\n"
     "  Provenance:\n"
     "    git log -L '/^foreach ($r in $adding)/,+1:scripts/claude-permissions.ps1'"),

    ("PROBE E: delete the post-commit check that no unrelated top-level key was lost",
     PROBE_SURVIVE,
     [(SCRIPT, PROBE_KEYS_NEW, PROBE_KEYS_OLD)],
     [],
     "Declared expectation: SURVIVES, for the same structural reason as PROBE D,\n"
     "  and worth stating precisely because the suite LOOKS like it covers this:\n"
     "  'grant preserves unrelated settings' asserts model and theme survive. It\n"
     "  passes here, because the keys really do survive -- the candidate\n"
     "  validator is what makes them survive, and this line only re-checks it\n"
     "  afterwards. A case that passes on both arms cannot be the defence.\n"
     "  Provenance:\n"
     "    git log -L '/^foreach ($k in $otherKeys)/,+1:scripts/claude-permissions.ps1'"),

    # ------------------------------------------------ round 6: R5, the three
    #                                                   guards with no mutant
    ("V2-a: BRANCH:unchanged states the discard again instead of reporting it",
     KILL,
     [(SCRIPT, V2_MSG_NEW, V2_MSG_OLD)],
     ["does not claim a discard it could not perform"],
     "R5. The V2 guard had a test case and no mutant, so the only evidence it\n"
     "  was real lived in a review document -- the same unmaintainable provenance\n"
     "  this file stopped relying on for line numbers. This puts the defect back:\n"
     "  the verdict says 'The candidate was discarded.' unconditionally, while\n"
     "  Remove-Item ran with -ErrorAction SilentlyContinue and may have done\n"
     "  nothing. Declared expectation: KILLED by the case that holds the\n"
     "  candidate open with FileShare::Read, which makes the deletion fail\n"
     "  silently while the destination is still byte-identical.\n"
     "  Provenance:\n"
     "    git log -L '/The candidate was discarded/,+1:scripts/claude-permissions.ps1'"),

    ("V2-b: move the deletions back BELOW the MEASURED table",
     KILL,
     [(SCRIPT, V2_ORDER_NEW, V2_ORDER_OLD),
      (SCRIPT, V2_ORDER2_NEW, V2_ORDER2_OLD)],
     ["and 'absent' via the same axis"],
     "R5, the other half of V2 and the reason the deletions moved at all. With\n"
     "  the deletions inside BRANCH:unchanged, the table prints 'candidate still\n"
     "  present .... <path>' and the very next statement deletes that file, so\n"
     "  the row is stale before the operator can read it. Two edits to the same\n"
     "  file, which is the case apply_edits was fixed to handle. Declared\n"
     "  expectation: KILLED by the F2 unchanged/absent case, which asserts the\n"
     "  row reads 'no'. A SECOND failure is expected and is a CASCADE, not an\n"
     "  independent finding: the F2 case drives 'unchanged' and 'absent' in one\n"
     "  Check, so throwing on the first leaves 'absent' unexercised and the\n"
     "  coverage case at the bottom says so. Read it that way, not as two\n"
     "  defects.\n"
     "  Provenance:\n"
     "    git log -L '/THE ONLY DELETIONS THIS FUNCTION PERFORMS/,+1:scripts/claude-permissions.ps1'"),

    ("V6-a: curate the allow list -- drop empty-string entries on a grant",
     KILL,
     [(SCRIPT, V6_EMPTY_NEW, V6_EMPTY_OLD)],
     ["EMPTY-STRING entry is preserved"],
     "R5. The two V6 controls rested on their test cases alone. This is the\n"
     "  tidy-up F4 refused: an empty string is not in this script's vocabulary,\n"
     "  so the script has nothing to say about it and no business removing it.\n"
     "  Silently dropping an entry an operator put there is a change to a\n"
     "  permissions file that nobody asked for and nothing announced. Declared\n"
     "  expectation: KILLED.\n"
     "  Provenance:\n"
     "    git log -L '/^    \\$wanted   = @(\\$existing \\+ \\$adding)/,+1:scripts/claude-permissions.ps1'"),

    ("V6-b: revoke removes only the FIRST copy of each owned rule",
     KILL,
     [(SCRIPT, V6_DUPE_NEW, V6_DUPE_OLD)],
     ["duplicate owned rules"],
     "R5, the second V6 control. A surviving duplicate is a standing\n"
     "  authorization the operator was told had been revoked, which is the OPS-6\n"
     "  failure this script exists to prevent -- worse than a failed grant,\n"
     "  because the failure is silent and reported as success. Declared\n"
     "  expectation: KILLED.\n"
     "  Provenance:\n"
     "    git log -L '/\\$OWNED -notcontains \\$_/,+1:scripts/claude-permissions.ps1'"),
]


def restore():
    for p, text in ORIG.items():
        io.open(p, "w", encoding="utf-8", newline="").write(text)


def run_tests(timeout=900):
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", TESTS],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, ["(the suite did not finish within %ds)" % timeout]
    failed = [l.strip() for l in r.stdout.splitlines() if "FAIL  " in l]
    return r.returncode, failed


def script_parses(path):
    """A random-line edit that stops the file parsing is not a mutant: every case
    would fail for a reason that has nothing to do with the line. Such a draw is
    DISCARDED and reported, never counted as a kill."""
    cmd = ("$e=$null;$null=[System.Management.Automation.Language.Parser]::ParseFile("
           "'%s',[ref]$null,[ref]$e); if($e.Count){'NO'}else{'YES'}" % os.path.abspath(path))
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True)
    return r.stdout.strip() == "YES"


def anchor_line(path, anchor):
    """The CURRENT 1-based line number of an anchor's first line, computed now.

    V4: every line number this file used to print had been typed by hand and had
    gone stale -- "line 663" and "line 714" matched no version of the script, so
    an auditor who opened them found unrelated code and had every reason to call
    the provenance invented. A number nobody maintains cannot go stale."""
    body = ORIG[path].replace("\r\n", "\n")
    i = body.find(anchor.replace("\r\n", "\n"))
    if i < 0:
        return None
    return body.count("\n", 0, i) + 1


def apply_edits(edits):
    """Apply every edit, accumulating per file. Returns True if all anchors
    matched exactly once.

    (This used to re-read the pristine text inside the loop, so a mutant with
    two edits to the SAME file silently kept only the last one. No mutant had
    two same-file edits until round 5, so it had never bitten -- which is the
    kind of thing a checker is supposed to notice about itself.)"""
    staged = {}
    for path, old, new in edits:
        body = staged.get(path, ORIG[path].replace("\r\n", "\n"))
        o = old.replace("\r\n", "\n")
        n = body.count(o)
        if n != 1:
            print("  ANCHOR in %s MATCHED %d TIMES -- skipped, proves nothing" % (path, n))
            return False
        staged[path] = body.replace(o, new.replace("\r\n", "\n"))
    for path, body in staged.items():
        io.open(path, "w", encoding="utf-8", newline="").write(body)
    return True


# ----------------------------------------------------------- random lines ----
# Inversions are tried in this order, longest token first, so that -notcontains
# is never matched as -contains.
INVERSIONS = [
    ("-notcontains", "-contains"), ("-contains", "-notcontains"),
    ("-notmatch", "-match"), ("-match", "-notmatch"),
    ("-isnot", "-is"), ("-is", "-isnot"),
    ("-notlike", "-like"), ("-like", "-notlike"),
    ("-ne", "-eq"), ("-eq", "-ne"),
    ("-ge", "-lt"), ("-lt", "-ge"),
    ("-le", "-gt"), ("-gt", "-le"),
    ("-and", "-or"), ("-or", "-and"),
]


def named_by_a_finding():
    """Every SCRIPT line any mutant above names. The random sampler draws from
    what is left, which is the operational definition of 'a line no finding in
    this review sequence has named'."""
    named = set()
    for _label, _mode, edits, _must, _note in MUTANTS:
        for path, old, _new in edits:
            if path != SCRIPT:
                continue
            for line in old.replace("\r\n", "\n").split("\n"):
                s = line.strip()
                if s:
                    named.add(s)
    return named


def candidate_lines():
    """Executable lines of the script: not blank, not a comment, not inside a
    <# #> block, and not named by any mutant above."""
    body = ORIG[SCRIPT].replace("\r\n", "\n").split("\n")
    named = named_by_a_finding()
    out = []
    in_block = False
    for lineno, raw in enumerate(body, start=1):
        s = raw.strip()
        if in_block:
            if "#>" in s:
                in_block = False
            continue
        if s.startswith("<#"):
            if "#>" not in s:
                in_block = True
            continue
        if not s or s.startswith("#"):
            continue
        if s in named:
            continue
        out.append((lineno, raw))
    return out


# Lines that carry no behaviour of their own: deleting one is a syntax error,
# never a defect, and every such draw would be spent on a parse check.
STRUCTURAL = set(["}", "{", ")", "(", "})", ")}", "};", "} else {", "} catch {",
                  "} finally {", "try {", "else {", "param(", "@(", "))"])


def mutate_line(raw):
    """One mechanical edit for an arbitrary line: invert the first comparison or
    logical operator on it, or, failing that, delete the line. Returns
    (new_text, description), or (None, why-not) for a line that carries no
    behaviour to change."""
    for a, b in INVERSIONS:
        idx = raw.find(" %s " % a)
        if idx >= 0:
            return raw[:idx] + (" %s " % b) + raw[idx + len(a) + 2:], "%s -> %s" % (a, b)
    s = raw.strip()
    if s in STRUCTURAL or s.endswith("{") or s.endswith("+") or s.endswith(",") or s.endswith("`"):
        return None, "structural or continuation line"
    return "# (line deleted by the random-line sampler)", "delete the line"


def random_line_pass(n, seed):
    """Sample N lines nobody named, mutate each BY LINE NUMBER, report the kill
    rate as a number. This does NOT set the exit code: a low kill rate is a
    measurement to publish, not a thing to fix by cherry-picking the lines that
    happened to be drawn."""
    body = ORIG[SCRIPT].replace("\r\n", "\n").split("\n")
    pool = candidate_lines()
    rng = random.Random(seed)
    rng.shuffle(pool)

    print()
    print("=" * 78)
    print("RANDOM-LINE SAMPLE  seed=%d  wanted=%d  pool=%d executable lines that no"
          % (seed, n, len(pool)))
    print("                    mutant in this file names")
    print("=" * 78)

    killed = surviving = structural = 0
    discarded = []
    drawn = 0
    for lineno, raw in pool:
        if drawn >= n:
            break
        new, how = mutate_line(raw)
        if new is None:
            structural += 1
            continue
        mutated = list(body)
        mutated[lineno - 1] = new
        io.open(SCRIPT, "w", encoding="utf-8", newline="").write("\n".join(mutated))
        if not script_parses(SCRIPT):
            restore()
            discarded.append(lineno)
            continue
        drawn += 1
        print()
        print("  LINE %d  (%s)" % (lineno, how))
        print("    was: %s" % raw.strip()[:100])
        print("    now: %s" % new.strip()[:100])
        code, failed = run_tests()
        restore()
        if code is None:
            killed += 1
            print("    --> KILLED (timeout): %s" % failed[0])
            continue
        for f in failed[:4]:
            print("      %s" % f[:104])
        if len(failed) > 4:
            print("      ... and %d more" % (len(failed) - 4))
        if code != 0:
            killed += 1
            print("    exit=%d  failures=%d  --> KILLED" % (code, len(failed)))
        else:
            surviving += 1
            print("    exit=%d  failures=%d  --> SURVIVED: no assertion in the suite "
                  "sees this line" % (code, len(failed)))

    print()
    print("=" * 78)
    print("RANDOM-LINE KILL RATE: %d of %d killed  (seed=%d)" % (killed, drawn, seed))
    if discarded:
        print("  %d further draw(s) DISCARDED because the edit stopped the script "
              "parsing" % len(discarded))
        print("  (lines %s) -- a parse error fails every case for a reason that has"
              % ", ".join(str(d) for d in discarded))
        print("  nothing to do with the line, so counting it as a kill would inflate")
        print("  this number.")
    if structural:
        print("  %d line(s) were passed over as structural: a closing brace or a"
              % structural)
        print("  continuation carries no behaviour of its own to change.")
    print("  This number does not set the exit code. It is the state of the suite")
    print("  outside the lines the findings named, and it is published as it comes.")
    print("=" * 78)
    return killed, drawn


# ------------------------------------------------------------------ main -----
argv = sys.argv[1:]
SEED = 20260826
SAMPLE = 12
run_declared = "--only-random" not in argv
run_random = "--no-random" not in argv
if "--seed" in argv:
    SEED = int(argv[argv.index("--seed") + 1])
if "--random-lines" in argv:
    SAMPLE = int(argv[argv.index("--random-lines") + 1])

print("=" * 78)
print("CONTROL  unmutated -- all must pass")
print("=" * 78)
code, failed = run_tests()
print("  exit=%s  failures=%d" % (code, len(failed)))
if code != 0:
    for f in failed:
        print("    %s" % f[:110])
    print("  CONTROL IS ALREADY FAILING; the mutations below would prove nothing.")
    sys.exit(1)

ok = True
if run_declared:
    by_mode = {}
    for _l, m, _e, _mf, _n in MUTANTS:
        by_mode[m] = by_mode.get(m, 0) + 1
    print()
    print("  %d declared mutants: %s" % (len(MUTANTS),
          ", ".join("%s=%d" % (k, v) for k, v in sorted(by_mode.items()))))

    for label, mode, edits, must_fail, note in MUTANTS:
        print()
        print("=" * 78)
        print("MUTANT  %s" % label)
        print("  expected: %s" % mode)
        for path, old, _new in edits:
            ln = anchor_line(path, old)
            print("  anchor:   %s line %s (computed now, never written down)"
                  % (path, ln if ln else "NOT FOUND"))
        for line in note.splitlines():
            print("  %s" % line)
        print("=" * 78)

        if not apply_edits(edits):
            ok = False
            restore()
            continue

        code, failed = run_tests()
        if code is None:
            print("    %s" % failed[0])
            print("  --> the suite did not finish; this mutant proves nothing")
            ok = False
            restore()
            continue
        caught = [f for f in failed if any(w.lower() in f.lower() for w in must_fail)]
        for f in failed:
            print("    %s" % f[:110])
        print("  exit=%d  total failures=%d  relevant failures=%d"
              % (code, len(failed), len(caught)))

        if mode in (KILL, PROBE_KILL):
            if code != 0 and caught:
                print("  --> KILLED: the defect is caught by the test written for it")
            else:
                print("  --> SURVIVED: the test does NOT catch this defect")
                ok = False
        elif mode == PROBE_SURVIVE:
            if code == 0 and not failed:
                print("  --> SURVIVED, AS DECLARED. This is a KNOWN GAP, reported as one:")
                print("      the suite is entirely blind to this edit. See the note above")
                print("      for why that is accepted rather than fixed.")
            else:
                print("  --> KILLED, contrary to the declaration. Something in the suite")
                print("      DOES defend this line; the note above is wrong and the gap")
                print("      claimed here does not exist.")
                ok = False
        else:
            if not caught:
                print("  --> SURVIVED AS EXPECTED: with the harness blinded, the defect")
                print("      that the exit-3 mutant killed is invisible. The exit-code")
                print("      plumbing is load-bearing.")
            else:
                print("  --> UNEXPECTED KILL: the named test still fails with the harness")
                print("      blinded, so something OTHER than the exit code is catching")
                print("      this. The control does not prove what it claims.")
                ok = False
        restore()

rate = None
if run_random:
    rate = random_line_pass(SAMPLE, SEED)

restore()
print()
print("=" * 78)
if run_declared:
    print("VERDICT: %s" % ("every declared mutant behaved as expected"
                           if ok else "AT LEAST ONE DECLARED MUTANT DID NOT BEHAVE AS EXPECTED"))
if rate is not None:
    print("         random-line kill rate: %d of %d" % rate)
print("  both files restored")
sys.exit(0 if ok else 1)
