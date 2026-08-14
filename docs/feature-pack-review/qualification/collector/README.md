# The qualification collector, under version control

`collect_shadow_evidence.py` here is the CANONICAL COPY of the script the
scheduled task "ScanHound Qualification Evidence" runs every 6 hours from
`X:\Docker Apps\scanhound-qualification-evidence\` on the server.

It lived ONLY on the server until 2026-08-14 — no history, no review surface,
edits proven only by a `.bak` beside the file. The same review that demanded it
be committed also found this bundle's own `selftest.py` had been silently
failing for three schema bumps because nothing runs it: unversioned and unowned
validation code rots.

## Copy discipline (transitional — peer review of PR #70)

- **Edits originate HERE, in git, only.** Deploy repo -> live; never live -> repo.
- **After every deploy, verify byte equality**:

      certutil -hashfile "X:\Docker Apps\scanhound-qualification-evidence\collect_shadow_evidence.py" SHA256
      certutil -hashfile "X:\Docker Apps\ScanHound\docs\feature-pack-review\qualification\collector\collect_shadow_evidence.py" SHA256

- The reviewed end-state is for the scheduled task to execute THIS file directly
  with the state/evidence directory passed in (parameterise `EVIDENCE`), so
  reviewed code IS the executed code. Until then, two copies + this discipline.

## Manifest scope

`../SHA256SUMS` covers the ORIGINAL qualification bundle only (AUTHORIZATION,
runbook, `scripts/`). The collector files here are deliberately outside it: the
collector carries live operational state and is expected to evolve between
qualification windows, while the manifest freezes the reviewed evidence bundle.

## Server-side state (never in git)

  - `stop-condition.last`  last DELIVERED notification state, JSON:
                           `{"state":"stop","signature":...}` or `{"state":"clear"}`.
                           Legacy plain-text parses as a delivered stop.
                           Advanced ONLY on confirmed Gotify delivery.
  - `gate-passed.notified` one-shot marker for the gate-passed alert (same rule)
  - `auth-token.txt`       readiness cross-check credential
  - `shadow-window.log`    the full every-run record (alerts are deduped;
                           this log never is)

The notification state machine (`marker_transition`) is pure and tested in the
repo suite: `tests/test_qual_collector_marker.py`.
