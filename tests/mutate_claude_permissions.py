"""Do the tests for scripts/claude-permissions.ps1 actually catch the defects
they were written for?

Passing tests prove nothing on their own. This puts each defect BACK and
requires the relevant test to FAIL. It is the successor to the scratchpad
checker written for round 2 (that one's mutant-1 anchor went stale when the
candidate write moved from WriteAllText to a FileStream); it added the two
mutants for the OPS-7 / SR2-3 round, and now three more for the round-3
defects: D1 (the failure handler asserting unmeasured state), D2 (the ReadOnly
regression) and D3 (the absent allow key).

Seven of the eight mutants are expected to be KILLED; one is expected to
SURVIVE BY DESIGN -- it is the control that proves WHICH edit does the killing.
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

    if mode == KILL:
        if code != 0 and caught:
            print("  --> KILLED: the defect is caught by the test written for it")
        else:
            print("  --> SURVIVED: the test does NOT catch this defect")
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
