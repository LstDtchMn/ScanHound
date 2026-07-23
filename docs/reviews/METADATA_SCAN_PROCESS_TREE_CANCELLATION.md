# Metadata Scan Process-Tree Cancellation

Base: `eee77f2f3a8ec36c98edf2be97dd60c873bdb445`

This change ports the intended metadata-scan cancellation behavior from PR #21
onto current main while replacing its direct-child-only subprocess shutdown.

Cancellable probes launch in an isolated POSIX session/process group. Cancel or
timeout sends SIGTERM to the complete group, closes the parent's stdout/stderr
read handles, then escalates surviving group members to SIGKILL. The runner
never calls `communicate()` after cancellation, so a descendant inheriting the
pipes cannot hold the caller open waiting for EOF.

Windows uses `CREATE_NEW_PROCESS_GROUP`, `CTRL_BREAK_EVENT`, and `taskkill /T`
with direct-child fallbacks. Non-cancellable callers retain the original
`subprocess.run(..., capture_output=True, timeout=...)` behavior.

The durable metadata scan passes its cancellation predicate through initial
ffprobe work, stream-detail probes, HDR10+ detection, and Dolby Vision
detection. A cancelled item returns to `pending` and does not increment
processed, succeeded, or failed counters.

No media write behavior, CAPTCHA handling, browser behavior, or download
pipeline behavior is changed.
