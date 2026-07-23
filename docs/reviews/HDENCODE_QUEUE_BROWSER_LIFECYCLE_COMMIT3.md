# ScanHound Commit 3 — HDEncode queue/browser lifecycle blockers

Base guard: `4659c3c1d596d79468e07b4b8aec80ef5984db33`
Target branch: `fix/hdencode-download-reliability`

## Policy decisions

- **B1:** `backend.browser_adapter` is the single authority for persistent-profile
  resolution. Docker invokes its cleanup CLI before ScanHound starts and removes only
  `SingletonLock`, `SingletonCookie`, and `SingletonSocket`.
- **B2:** cancellation is rejected once an item is `claimed`; the API returns a typed
  HTTP 409. Worker terminal writes are also owner/state compare-and-set operations.
- **B3:** the queue remains one worker in one process. A separate watchdog enforces the
  existing claim lease. An expired owned claim is persisted as an unknown-outcome manual
  failure, then the process exits with code 70 so Docker's `restart: unless-stopped`
  rebuilds the worker. The uncertain row is never automatically rescheduled, avoiding
  duplicate JDownloader delivery.
- **B4:** source-wide pause now defers only remaining rows in the same batch whose
  `source` matches the triggering item. Other sources remain claimable.

No CAPTCHA solving, challenge interaction, proxy rotation, or fingerprint evasion is
introduced. No schema or frontend files are changed.
