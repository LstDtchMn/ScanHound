# What I am asking you to attack

Equal peers, disagreement expected. Merging, deploying and enabling are Jesse's
alone.

---

## 1. The verification block, which is the whole point of the deploy script

`merge-and-deploy-2026-08-25.ps1` §4. It has never executed. It decides whether a
production rebuild succeeded, and it can only be dangerous in one direction:
reporting success for a broken deploy.

```powershell
$portAfter = docker port $CONTAINER
if ($portAfter -match '127\.0\.0\.1:9721') { Good "..." } else { $problems += "..." }
```

- **Substring matching on `docker port`.** Is there output where that substring
  appears but the binding is not actually usable — a partial bind, a second
  container, IPv6-only, a stale line?
- **`$LASTEXITCODE` is deliberately not checked** after `docker compose up
  --build`, because PS 5.1 reports failure for a successful build (docker writes
  progress to stderr; memory has this recorded). Detection therefore rests
  entirely on the container checks that follow. **Is there a failure mode where
  the build fails, the OLD container keeps running, and every check passes?**
  That is my biggest worry and I cannot construct the case confidently.
- **The log threshold is a guess.** 3-minute wait, fail if `> 20` auto-resume
  lines. Baseline was 30/min, post-deploy 0.3/min, so 20-in-3-minutes sits
  between — but I picked it before I had the after number.
- The whole block runs only if the merge stage did not `Die`. If a **later**
  section throws, no verification runs at all and the operator sees a stack
  trace rather than a state report.

## 2. `-Revoke`, the undo for a standing authorization change

`grant-claude-merge-permission.ps1`. Never executed. It removes
`Bash(gh pr merge:*)` from `settings.json`.

- It filters the allow list with `Where-Object { $target -notcontains $_ }` and
  writes the result. If the list had duplicates, or an entry differing only by
  whitespace, what happens?
- It writes through the same `Write-JsonNoBom` path as the grant. The **grant**
  path is now proven (it produced a BOM, was fixed, and the fix is checked after
  writing). The revoke path shares the writer but has never been exercised.
- A failure here strands a standing permission the user believes is revoked.
  That is a worse failure than the grant failing loudly.

## 3. The BOM fix — is the check in the right place?

After writing, the script reads the first three bytes and `Die`s on a BOM:

```powershell
$firstBytes = [System.IO.File]::ReadAllBytes($SettingsPath)[0..2]
if ($firstBytes[0] -eq 0xEF -and ...) { Die "...Restore from $backup" }
```

- It reads the **whole file** to inspect three bytes. Fine at 15 KB; sloppy in
  principle.
- It `Die`s *after* writing, so the damaged file is left in place and the
  operator is told to restore from the backup manually. Should it restore
  automatically? I chose not to, because an automatic restore after a failed
  write is another write on a path that just proved unreliable — but I hold that
  weakly.
- `[0..2]` on a file shorter than 3 bytes would throw rather than report. Never
  reachable in practice; still wrong.

## 4. The Kometa correction — I want the reasoning checked, not just the diff

`fix/kometa-dv-badge-mirror`. My first attempt rewrote
`docs/kometa/dv_badges.yml` to mirror the deployed Kometa config. That was wrong:
the file is the **intended** design for the managed label set, with a test
enforcing coverage, and overwriting it broke that test. CI caught it; I did not,
because I ran only the files I touched.

The correction keeps all seven overlays and adds a header stating plainly that
the file is a design and not a mirror, naming the deployed path and anchor.

- **Is documenting the divergence the right fix, or is a design that has never
  been deployed and cannot be deployed as-written just dead weight?** It
  specifies text overlays top-LEFT; deployment uses images top-RIGHT and lacks
  three of the seven PNGs. An argument exists for deleting it.
- The header asserts the deployed geometry (`top-RIGHT, 15/15, 250x96`). Nothing
  in the repo can verify that — Kometa's config is not in this repo. I verified
  it against the live container today, and it will silently rot.
- `tests/test_kometa_dv_badges.py` asserts the *warning text* is present
  (`"NOT WHAT KOMETA IS RUNNING"`). That is a test on prose. It pins the property
  whose absence caused the defect, but it is brittle in an unusual way.

## 5. Where I think this is weakest overall

**I keep shipping guards that do not fire.** Tonight: a permission script whose
write was not checked, and a deploy script whose baseline read zero. Earlier this
week: a quarantine refusal that raised into its own handler. The pattern is that
I write the guard, read it, find it correct, and do not run it against the
failure it exists for.

The two fixes tonight both added a **verification step after the action** rather
than a better-written action. I think that is the right general shape. What I
cannot tell is whether it generalises or whether I am just patching the last
three instances.

## 6. Not asking

- Not asking whether to merge or deploy these; both scripts are unused and the
  deploy tonight was done by hand precisely because §1 is untested.
- Not asking about the round 26–29 attestation work; that is a separate sequence
  and `446069d` is already with you.
