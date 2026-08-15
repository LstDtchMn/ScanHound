# Peer round: why did the JDownloader poll stall for ~15 hours?

**Review the REASONING, not a diff.** No code is proposed yet — deliberately.
This is inference from indirect evidence, which is exactly where I have been
wrong today, so I want the hypothesis attacked before I build on it.

## What is PROVEN

**1. A JDownloader restart does NOT cause a persistent stall.** Reproduced
under observation today (16:00 local), with the telemetry from #77 deployed:

    16:00:04  stalled=4s  fails=0
    16:00:19  stalled=19s fails=1  phase=query_packages  "No connection established"
    16:00:34  RECOVERED after 15s
    16:01:04  stalled=5s  fails=0

One failed poll, at the package query, then clean recovery in 15 seconds. So
the reconnect path works, and "JD restarted and ScanHound could not recover"
is REFUTED as the explanation for the 2026-08-15 outage.

**2. During the real outage, the failures were NOT at `query_packages`.** That
path already logged `logger.warning("JD package poll failed: %s", e)` before any
of today's changes. Across ~20 hours of logs covering the outage there is
exactly ONE such warning. A poll failing there every cycle would have produced
thousands.

By elimination the repeated failures were in `_connect_jd_device()`, which was
wrapped in a bare `except Exception: return []` — no log at any level. That is
consistent with one initial `query_packages` failure (the stale handle, logged
once), the cache being invalidated, and every subsequent RECONNECT failing
silently thereafter.

**3. The retry rate has no backoff.** The results poller runs every
`interval = 8.0` seconds (`_start_results_poller`, backend/api/main.py:353). A
failed poll calls `_invalidate_jd_cache()`, so the next cycle performs a FULL
re-authentication: `myjdapi.Myjdapi()` -> `jd.connect(email, password)` ->
`jd.update_devices()` -> `jd.get_device(...)`. The 90s `_JD_CONN_TTL` does not
apply, because the cache was cleared rather than aged out.

Over a 15-hour outage that is roughly **6,750 full authentications** against
the MyJDownloader cloud service — about 450/hour, sustained.

## The hypothesis

The retry loop is self-sustaining: repeated authentication triggers
rate-limiting or a temporary block at MyJDownloader; the block causes the next
poll to fail; the failure triggers another authentication 8 seconds later,
which sustains the block. It ends only when something breaks the loop —
which is exactly what restarting the container does.

It accounts for every observation:

| Observation | Accounted for |
|---|---|
| reconnect works normally | proven above; a blip recovers in seconds |
| one `query_packages` warning, then silence | first failure logged, subsequent connect failures silent |
| outage measured in HOURS, not seconds | a sustained block, not a transient error |
| a container restart reliably "fixes" it | the hammering stops long enough for the block to lapse |
| it recurs (Jesse reports this pattern) | nothing prevents the loop re-forming |

## What is NOT proven

* That MyJDownloader actually rate-limits or blocks in this way. I have not
  seen the error text from a real occurrence — the path was silent, which is
  the whole reason this is inference. #77's telemetry now records
  `failure_phase` and `last_error`, so the next occurrence should name it.
* That authentication was the failing step specifically, versus
  `update_devices` or `get_device`. The elimination argument gets us to
  "inside `_connect_jd_device`", not to which line.
* Whether anything ELSE was different that night (a network event, a
  MyJDownloader service incident, a credential change). I have no evidence
  either way, and the reproduction only rules out the restart itself.

## The fix I would build

Exponential backoff on the connect path — roughly 8s, 16s, 32s ... capped at a
few minutes — so a failing reconnect cannot re-authenticate ~450 times an hour.
Reset on success.

I believe this is correct **regardless of whether the hypothesis is right**:
re-authenticating every 8 seconds indefinitely is wrong behaviour toward any
remote service. The hypothesis only determines whether it also FIXES the
outage or merely stops making it worse.

## Questions I want pressed

1. **Is the elimination argument sound?** It rests on "the old code logged
   query_packages failures at WARNING, and we saw one". Is there a path where
   repeated `query_packages` failures would NOT have logged — a different
   exception type, a logger config, an early return I have missed?
2. **Is backoff on the connect path safe** given the poller also serves the
   WebSocket downloads view? A capped backoff means the UI can be up to a few
   minutes stale after a real outage ends. Acceptable, or should a user-visible
   action (opening the Downloads page) force an immediate retry?
3. **Is there a better explanation** for a 15-hour failure that a 15-second
   reproduction cannot produce? I am reasoning from absence of evidence in a
   path that was silent by construction, and I would rather have that
   challenged now than build on it.
4. **Should the fix distinguish failure phases** in its backoff? An auth
   rejection and a device-not-found probably warrant different handling, but I
   have no data on which occurs.

## Context

Deployed today: #72, #75, #76, #77 (see `2026-08-15-state-of-play.md`). The
telemetry referenced here is live and verified — `/health` currently reports
`jd_poll` with `stalled_seconds`, `consecutive_failures`, `failure_phase` and
`last_error`.
