"""Do the tests for scripts/claude-permissions.ps1 actually catch the defects
they were written for?

Passing tests prove nothing on their own. This puts each defect BACK and
requires the relevant test to FAIL. It is the successor to the scratchpad
checker written for round 2 (that one's mutant-1 anchor went stale when the
candidate write moved from WriteAllText to a FileStream); it added the two
mutants for the OPS-7 / SR2-3 round, and now three more for the round-3
defects: D1 (the failure handler asserting unmeasured state), D2 (the ReadOnly
regression) and D3 (the absent allow key).

Round 4 adds six more (F1..F6) and, in answer to the charge that this file had
become a RECEIPT for things just fixed rather than a search, two PROBES aimed at
lines nobody in the review sequence has touched -- both unmodified since
8ae7837, the commit that created the script. A probe declares its expected
outcome up front and the run is checked against the declaration in BOTH
directions, so a declared survivor is reported as a KNOWN GAP instead of being
quietly absorbed.

Seventeen of the nineteen mutants are expected to be KILLED; two are expected to
survive -- one is the control that proves WHICH edit does the killing, the other
is PROBE B, a real and named gap.
Run from the repository root:

    python tests/mutate_claude_permissions.py
"""
import io
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

REVOKE_NEW = """    $wanted   = @($existing | Where-Object { $OWNED -notcontains $_ })
    $removing = @($existing | Where-Object { $OWNED -contains $_ })"""
REVOKE_OLD = """    $scoped   = if ($IncludeDeploy) { $MERGE_RULES + $DEPLOY_RULES } else { $MERGE_RULES }
    $wanted   = @($existing | Where-Object { $scoped -notcontains $_ })
    $removing = @($existing | Where-Object { $scoped -contains $_ })"""

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
F4_NEW = """$allowValue = $settings.permissions.allow
if ($allowValue -isnot [System.Array]) {
    Die ("settings.json has a 'permissions.allow' that is not a JSON array (it is a " +
         $allowValue.GetType().Name + "). This script will not guess what you meant or " +
         "rewrite it for you. Make it an array of rule strings -- e.g. " +
         [char]34 + 'allow' + [char]34 + ': [] -- and re-run. Nothing was written.')
}
for ($i = 0; $i -lt $allowValue.Count; $i++) {
    if ($allowValue[$i] -isnot [string]) {
        $what = $(if ($null -eq $allowValue[$i]) { 'null' } else { $allowValue[$i].GetType().Name })
        Die ("settings.json has a 'permissions.allow' entry at index $i that is not a string " +
             "(it is $what). A permission rule is a string; this script will not count, " +
             "reorder or rewrite a non-string entry. Fix or remove that entry and re-run. " +
             "Nothing was written.")
    }
}
"""
F4_OLD = """# (allow type guard removed by the mutation checker)
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

# F2. Grow the handler a seventh verdict branch that no test drives. The
# coverage case is the only thing in the suite that can notice.
F2_NEW = """        # BRANCH:altered
"""
F2_OLD = """        # BRANCH:altered
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
# Both have been unmodified since 8ae7837, the commit that created the file.
# Neither was named by OPS-1..7, SR2-1..3, D1..D3 or F1..F6.

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

    ("F2: give the handler a seventh verdict branch that no test drives",
     KILL,
     [(SCRIPT, F2_NEW, F2_OLD)],
     ["every verdict branch in the handler is named by the axis"],
     "Half the handler's decision surface had no assertion and no mutant, and\n"
     "  nothing in the suite could SAY so -- that is the finding, not the two\n"
     "  missing tests. This mutant is the finding itself: a branch appears, and\n"
     "  the coverage case must report that the failure-mode axis cannot name it.\n"
     "  If this survives, the axis is decoration."),

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
     [(SCRIPT, F4_NEW, F4_OLD)],
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

    # -------------------------------------------------- untouched-line probes
    ("PROBE A: the backup becomes an EMPTY file"
     " (scripts/claude-permissions.ps1 line 663, untouched since 8ae7837)",
     PROBE_KILL,
     [(SCRIPT, PROBE_BACKUP_NEW, PROBE_BACKUP_OLD)],
     ["HOLDS THE PRE-CHANGE BYTES"],
     "Aimed at a line no finding in this sequence named. Until the F5 sweep the\n"
     "  backup case asserted only that a .bak-* file EXISTS, so an empty backup\n"
     "  passed -- while every 'recover with: Copy-Item' the failure handler\n"
     "  prints names exactly that file. Declared expectation: KILLED by the byte\n"
     "  comparison added in this round."),

    ("PROBE B: delete the post-commit BOM check on the LIVE file"
     " (scripts/claude-permissions.ps1 line 714, untouched since 8ae7837)",
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
     "  it, and pretending otherwise is what this file exists to prevent."),
]


def restore():
    for p, text in ORIG.items():
        io.open(p, "w", encoding="utf-8", newline="").write(text)


def run_tests():
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", TESTS],
        capture_output=True, text=True)
    failed = [l.strip() for l in r.stdout.splitlines() if "FAIL  " in l]
    return r.returncode, failed


print("=" * 78)
print("CONTROL  unmutated -- all must pass")
print("=" * 78)
code, failed = run_tests()
print("  exit=%d  failures=%d" % (code, len(failed)))
if code != 0:
    for f in failed:
        print("    %s" % f[:110])
    print("  CONTROL IS ALREADY FAILING; the mutations below would prove nothing.")
    sys.exit(1)

ok = True
for label, mode, edits, must_fail, note in MUTANTS:
    print()
    print("=" * 78)
    print("MUTANT  %s" % label)
    print("  expected: %s" % mode)
    for line in note.splitlines():
        print("  %s" % line)
    print("=" * 78)

    applied = True
    for path, old, new in edits:
        body = ORIG[path].replace("\r\n", "\n")
        n = body.count(old)
        if n != 1:
            print("  ANCHOR in %s MATCHED %d TIMES -- skipped, proves nothing" % (path, n))
            applied = False
            ok = False
            break
        io.open(path, "w", encoding="utf-8", newline="").write(body.replace(old, new))
    if not applied:
        restore()
        continue

    code, failed = run_tests()
    caught = [f for f in failed
              if any(w.lower() in f.lower() for w in must_fail)]
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
            print("      that mutant 4 killed is invisible. The exit-code plumbing is")
            print("      load-bearing.")
        else:
            print("  --> UNEXPECTED KILL: the named test still fails with the harness")
            print("      blinded, so something OTHER than the exit code is catching")
            print("      this. The control does not prove what it claims.")
            ok = False
    restore()

restore()
print()
print("=" * 78)
print("VERDICT: %s" % ("every mutant behaved as expected"
                       if ok else "AT LEAST ONE MUTANT DID NOT BEHAVE AS EXPECTED"))
print("  both files restored")
sys.exit(0 if ok else 1)
