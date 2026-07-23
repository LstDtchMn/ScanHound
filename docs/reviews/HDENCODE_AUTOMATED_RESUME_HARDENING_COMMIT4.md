# ScanHound Commit 4 — automated-resume redelivery hardening

Base guard: `2e5389c0de0a766d60ce4dab071b0137e72719e2`
Target branch: `fix/hdencode-download-reliability`

## Safety correction

Automated cooldown resume now reactivates only `verification_required` and
`waiting_source` rows from the source associated with the current batch pause. Generic
`failed` rows are never selected automatically, and both automated resume and the final
claim vector exclude `operation_timeout_unknown` and `interrupted_unknown_outcome`.
A deliberate manual retry clears that poison marker, preserving operator-authorized retry.

A paused batch without a valid cooldown timestamp no longer auto-resumes immediately.

## Bounded hardening

- Watchdog timeout notifications use a bounded synchronous WebSocket flush before the
  fail-stop `os._exit(70)` call.
- Manual retry/resume paths refresh batch counters in the same transaction.
- The redundant pre-refresh deferred counter increment and dead restart `grace` variable
  are removed.
- `download_queue_claim_lease_seconds` is a validated setting with default `600` and bounds
  `60..7200`; the queue construction receives the configured value.

The 600-second default and whole-container fail-stop policy are intentionally unchanged.
No CAPTCHA solving, challenge interaction, proxy rotation, or fingerprint evasion is added.
