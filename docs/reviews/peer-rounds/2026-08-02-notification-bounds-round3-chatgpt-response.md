# ScanHound Peer Review — Notification Bounds Round 3

**Review date:** 2026-08-02  
**Repository:** `LstDtchMn/ScanHound`  
**Branch:** `claude/nostalgic-brattain-946f4f`  
**Branch head inspected:** `332c75f9eb1e4f32b9d581e19490561004a684a8`  
**Code commits reviewed:** `f5139b2`, `7e40791`, `1b353dc`  
**Review range:** `c8a60bf..1b353dc`  
**Review package:** `docs/reviews/peer-rounds/2026-08-02-notification-bounds-round3-for-chatgpt.md`

## Evidence boundary

I inspected the requested branch at the stated head. The head commit after
`1b353dc` contains only the round-3 review package, so the implementation verdict
below applies to the requested code range.

I independently ran the notification backend, notification bridge, bridge
lifecycle, and notification manager tests on Python 3.12.9:

```text
171 passed, 2 warnings
```

I also exercised the deployed configuration mapping and confirmed:

```text
desktop_default True
desktop_enabled_forwarded True
smtp_timeout_in_default False
smtp_timeout_forwarded <missing>
```

Finally, I ran a child-process probe in which a plain daemon outer thread created
a `ThreadPoolExecutor` worker and then the main thread returned. The process was
still alive after two seconds. This distinguishes the outer thread's daemon flag
from the exit behavior of resources created by its call graph.

I did not independently rerun the reported full 3.11/3.12 Docker matrix. The
reported full-suite results are consistent with the targeted results and do not
change the findings below.

---

# Verdict

## CHANGES REQUIRED BEFORE MERGE

Gate item 3 is correctly implemented and can close: outstanding notification
tasks are cancelled and gathered before asynchronous-generator shutdown,
default-executor shutdown, and loop close. The new non-executor-awaitable test
pins the failure mode that was missing in round 2.

Gate item 1 remains open. SMTP is materially improved for a silent connected
server, but the desktop path is still unbounded and the risk acceptance rests on
an incorrect statement that desktop notifications default to off. The actual
application default is `True`, is explicitly asserted by an existing test, and
is forwarded by the production `NotificationBridge` at startup. There is also a
second direct plyer call in the settings test endpoint that this round did not
inventory.

The new daemon wording also needs one qualification. CPython does not wait for a
plain daemon `Thread` merely because that outer thread is alive. It can still
initiate an executor worker, non-daemon child, child process that shutdown
machinery waits for, or exit hook that holds process termination. That is
already true of registry-owned ScanHound targets, so the outer thread's type
alone does not make abandonment safe.

Send-admission closure and the three stated lifecycle P0s remain outside this
range and are not re-reviewed here.

---

# Priority findings

## P1 — The desktop residual is enabled by default, not opt-in

The acceptance rationale added at `backend/notifications.py:168` says reaching
the unbounded plyer call requires an optional channel because
`desktop_notifications` defaults to off. The deployed configuration says the
opposite:

- `backend/config.py:592` sets `"desktop_notifications": True`;
- `tests/test_config.py:200` explicitly pins that value as the expected default;
- `backend/api/main.py:115-116` passes the loaded application configuration to
  `NotificationBridge.configure()`;
- `backend/notification_bridge.py:43-44` maps that true value to
  `desktop_enabled=True`.

The `config.get(..., False)` fallback in the bridge is only used when the key is
absent. It does not override `_DEFAULT_CONFIG`, which always supplies the key.
The pre-existing "default OFF" bridge comment is stale for the same reason.

On the headless Docker image, absence of both probed binaries may prevent the
channel from becoming actionable. That reduces exposure in that one deployment;
it does not support a repository-wide process-termination guarantee. On a
desktop build with plyer and a backend present, the unbounded executor callable
is enabled on a fresh configuration.

There is also a second live desktop dispatch at
`backend/api/routes/settings.py:322-328`. The `/settings/test/desktop` handler
calls `plyer_notif.notify()` directly. Its `timeout=5` is the notification's
display duration, not an execution timeout. Because this is a synchronous
FastAPI handler, the call runs in an AnyIO worker rather than the notification
loop's executor, but a hung worker still leaves the request and graceful
shutdown path unbounded. A shared bounded dispatcher must cover both this
endpoint and `DesktopNotificationChannel.send()`; fixing only the channel would
leave the test action unbounded.

Documentation cannot close the round-2 gate because the documented condition is
both supported and enabled by default.

## P1 — "Plain daemon" is not a sufficient safety classification for the tracked work

The primitive boundary is real: CPython's normal thread shutdown does not join a
plain daemon thread, while `concurrent.futures.thread._python_exit()` joins
registered executor workers irrespective of their daemon flag.

The stronger wording now in `backend/api/dependencies.py:35-42` and
`backend/api/dependencies.py:302-305` does not follow for the actual registry,
however. `join_lifespan_threads()` records only the outer thread. It does not
establish that the target's reachable call graph owns no executor or other
process-joined resource. Current counterexamples include:

- `backend/api/routes/scanner.py:370-371` starts `scan-run` as a registry-owned
  daemon; `ScannerService.run_scan()` creates an asyncio default executor through
  `run_in_executor()` and `_process_posts()` creates a `ThreadPoolExecutor` at
  `backend/scanner_service.py:1011`;
- `backend/api/routes/background.py:53` starts `BackgroundScanner.scan_once()`
  through the same registry and reaches the same scanner executors;
- `backend/plex_metadata_scan.py:198-215` starts registry-owned work whose target
  creates a `ThreadPoolExecutor`.

If one of those descendants is wedged when the outer daemon is abandoned, the
outer thread is not what CPython waits for, but process exit can still block.
The safe statement is therefore:

> CPython does not join the surviving outer threads solely because they are
> daemons. Process exit is guaranteed only if their reachable work has not
> created a non-daemon thread, registered executor worker, subprocess/child
> process with an exit wait, or another shutdown hook that can block.

This does not invalidate the shared join budget; it invalidates using the outer
thread's daemon flag as proof that the budget makes process exit safe. The log
should report the limited fact rather than claim safe abandonment.

## P2 — The advertised SMTP configuration override is not wired to production

`NotificationManager.configure_from_dict()` accepts `smtp_timeout`, but the
production bridge copies only:

```python
("smtp_host", "smtp_port", "smtp_username", "smtp_password",
 "email_from", "email_to", "smtp_tls")
```

at `backend/notification_bridge.py:60-63`. `smtp_timeout` is omitted. It is also
absent from the `AppConfig` notification fields, `_DEFAULT_CONFIG`, the settings
update model, and the frontend settings type/UI.

Consequently, an application configuration containing `smtp_timeout=0.25`
still constructs the deployed channel with the 30-second default. The new tests
instantiate `EmailChannel` directly or call `NotificationManager` directly, so
they do not exercise this mapping gap.

If the timeout is intended to be configurable, add it to the full configuration
path and validate it as a finite, positive, bounded number. `None`, zero,
negative values, `NaN`, and unreasonably large values must not silently restore
an ineffective process bound. If it is intentionally constant, remove the
config-override claim and the unused lookup.

## P2 — SMTP now has a socket-inactivity timeout, not one 30-second transaction deadline

Passing `timeout=` to `smtplib.SMTP` and `SMTP_SSL` correctly fixes the tested
black-hole case: a server that accepts and sends no greeting no longer blocks
forever. Both constructors are covered, and the direct timing tests demonstrate
that the value reaches the socket.

The package and code comments should not describe that as one timeout covering
the complete connect/TLS/auth/send chain in total wall-clock time. It is applied
to socket operations. Separate SMTP exchanges can each consume the timeout, a
peer can make progress slowly enough to reset inactivity waits, and hostname
resolution occurs before a connected socket carries that timeout. Thus 30
seconds is neither a total dispatch bound nor the later application-wide
deadline.

For this round, I accept the narrower result: the previously demonstrated silent
connected-server hang is fixed. Threading a shrinking application deadline
through the whole operation—or isolating it when a hard deadline must include
resolution—belongs with the explicitly deferred application-wide deadline work.
The wording and tests should preserve that narrower contract.

---

# Answers to Q14–Q16

## Q14 — Correct at the thread primitive boundary; incomplete for ScanHound's call graphs

A truly plain daemon thread with no process-joined descendants or shutdown-hook
dependencies is not joined during ordinary CPython interpreter shutdown. In that
narrow sense, the measured boundary is correct.

A daemon outer thread can still block exit indirectly by creating a
`ThreadPoolExecutor`, spawning a non-daemon thread or child process that is
waited on, registering an exit hook, or holding a resource an exit hook needs.
ScanHound already has registry-owned daemon targets that create executors, so
this is not merely a hypothetical future-worker caveat.

**Disposition:** accept the CPython mechanism; reject "the tracked threads are
plain daemons, therefore abandonment is safe" as a lifecycle guarantee. Narrow
the comments and log to the outer-thread fact and audit the reachable worker
types separately in the lifecycle rounds.

## Q15 — Not acceptable as the closure of gate item 1

The residual would still violate the process-level contract even if the feature
were opt-in. Here it is additionally enabled by the authoritative default and
has a second unbounded test-route call site.

Of the proposed fixes, prefer a **killable subprocess boundary** shared by both
desktop call sites. A small helper process can retain plyer's cross-platform
backend selection while the parent enforces a wall-clock timeout. On expiry the
parent must terminate, then kill, and reap it. On platforms where plyer launches
another executable, the boundary must also own and kill the descendant process
group/tree; killing only the immediate helper can orphan the real hung backend.

Direct `subprocess.run(timeout=...)` is reasonable only if ScanHound deliberately
reimplements each supported native command and also handles descendant cleanup.
It is not a complete cross-platform replacement by itself, and running that wait
inside the same default executor merely turns the permanent wedge into a finite
executor delay.

An alternative policy is to remove desktop notification support from the
process-termination contract and ship it disabled in every supported artifact.
That would require changing the authoritative default, UI default, claims, and
qualification scope. It is not what the current code does.

**Disposition:** gate item 1 remains open; bound both desktop call sites before
claiming it closed.

## Q16 — Add a positive SMTP process-exit test; do not pin the known exposure

There is a useful subprocess test that does not assert the desktop defect:

1. The parent test starts the accepting, silent SMTP server.
2. A child Python process configures the real `NotificationBridge` with email
   enabled and a short validated `smtp_timeout`.
3. The child submits a notification; the parent waits until the server has
   accepted the connection, proving the executor callable is already blocked in
   SMTP rather than cancelled before it starts.
4. The parent tells the child to call `shutdown()` and return from main.
5. The parent requires a clean child exit within a small outer deadline. On
   failure, the parent kills and reaps the child so the test suite cannot hang.

That proves the behavior gate item 1 actually cares about: a live SMTP executor
operation cannot hold interpreter exit indefinitely. It also forces the
currently missing `smtp_timeout` bridge propagation to work. Keep the existing
direct socket timing tests; they diagnose the lower-level reason if the process
test fails.

After desktop isolation lands, add the analogous child-process test with a
helper deliberately blocked forever and assert that the parent timeout kills it
and the application process exits. There is no value in a permanent regression
test whose expected result is "the product still hangs."

**Disposition:** yes, add the SMTP-specific positive process-termination test in
this gate; add the desktop equivalent with the desktop fix.

---

# Accepted work

## Gate item 3 is closed

The teardown order at `backend/notification_bridge.py:133-145` is correct for
the requested defect:

1. snapshot unfinished tasks on the owned loop;
2. cancel them;
3. gather them with `return_exceptions=True`;
4. shut down asynchronous generators;
5. shut down the default executor;
6. close the loop in `finally`.

The new test blocks a non-executor coroutine on `asyncio.sleep(300)`, observes
`CancelledError`, verifies the concurrent handle is resolved, and verifies the
loop closes. That is the deterministic test requested in round 2.

One wording precision: this mirrors the cancellation-and-gather portion of
`asyncio.run()`, not every diagnostic detail of its private cleanup helper.
That distinction does not reopen the gate item.

---

# Final disposition

| Question / item | Verdict |
|---|---|
| Q14 — plain daemon versus executor worker | **Mechanism accepted; current safety generalization rejected because tracked daemon targets already create executors.** |
| Q15 — documented desktop residual | **Reject as gate closure. Desktop is enabled by default and has two unbounded call sites. Use a shared killable subprocess boundary.** |
| Q16 — subprocess termination test | **Add an SMTP-specific positive child-process exit test now; add the desktop equivalent with its fix.** |
| Gate item 1 — backend process bounds | **Open. SMTP silent-server case improved; desktop remains unbounded.** |
| Gate item 2 — send-admission closure | **Deferred to the next round, as scoped.** |
| Gate item 3 — cancel/gather pending tasks | **Closed.** |
| Three lifecycle P0s | **Untouched and outside this review range.** |

## Merge gate for this round

Before this round is mergeable as the closure of gate items 1 and 3:

1. replace both direct desktop plyer call sites with one killable, wall-clock-
   bounded dispatcher, or explicitly remove desktop from the supported contract
   and make it disabled by default everywhere;
2. wire and validate `smtp_timeout` through the deployed bridge/configuration
   path, or make it an intentional constant and remove the override claim;
3. add the SMTP subprocess process-exit regression described under Q16;
4. narrow the daemon comments/log so they do not infer process safety from only
   the outer registered thread's daemon flag.

The send-admission closure, single application-wide deadline, generation
fencing, and `begin_lifespan()` remain the explicitly separate later work.
