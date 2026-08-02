# Handoff — RSS cancellation flake (closed) + the thread leak it exposed (open)

**Status:** the chartered de-flake task is **done**, fixed by another agent chat
in `6d067e2`. This worktree (`nice-meitner-2b717b`) made no code edits. What
follows is the diagnosis, its evidentiary limits, and guidance for the open item.

---

## What was fixed

`tests/test_feature_pack_integration.py::test_new_lifespan_cancels_waiting_rss_before_transport_construction`

Fixed on both agent branches, but **as two different SHAs** — the change was
applied separately to each, so there is no single commit id to cite:

| branch | SHA |
|---|---|
| `agent/rename-safety-gate` | `6d067e2` |
| `agent/hybrid-sweep-implementation` | `9b059c5` |

Both are pushed (local `agent/hybrid-sweep-implementation` == `origin/` at
`9ad1e4a`). Verified identical by `git patch-id --stable`: both yield
`fba0caf2314217f086b0b4c7e63ae5d916b6545a`. Cite whichever branch you are on —
looking for `6d067e2` on `hybrid-sweep-implementation` finds nothing and
wrongly suggests the fix is missing.

The fix records scraper constructions by
`threading.current_thread().name` and asserts against the worker thread
specifically, rather than asserting a process-global list is empty.

The guard is intact — the commit's mutation table shows
`real regression -> FAIL`, which is the line that matters, since de-flaking by
deleting a guard would also make a test stop failing.

**Do not reimplement. Rebase onto `6d067e2`.**

---

## Cause, and how strongly each part is attested

### Observed

A thread-liveness probe over the failing subset
(`-k 'full_disc or policy or scanner or rss or feed'` — 296 passed, 4 skipped,
3902 deselected, 127s) in a throwaway container showed:

- `tests/test_api_routes.py::TestScanner` leaks `jd-results-poller` and
  `poster-backfill`, one pair per `client` fixture instantiation, ~9 of each by
  the end of the class.
- The five `test_scan_start*` tests each leak a `Thread-N (_run_scan)`.
- Also leaked: a `ThreadPoolExecutor-0_*` pool and an `asyncio_0` portal.
- Those threads are still enumerated as alive during
  `tests/test_background_scanner.py` — the file immediately preceding
  `test_feature_pack_integration.py` in collection order.

A second, independent probe (committed at `tests/tools/threadleak.py`, run with
`-p threadleak`) found the same class of leak across 16 tests and named five
more lifespan-owned threads: `maintenance`, `notif-loop`, `download-queue`,
`download-queue-watchdog`, `background-scanner`.

### Inferred, not observed

The chain from leaked thread to failed assertion:

`transport.cloudscraper.create_scraper` is patched **process-globally** by the
test, so any live thread's construction lands in its list. The leaked threads
reach `cloudscraper.create_scraper()` with no arguments via
`backend/scanner_service.py` (`:482` on `main`, `:476` on the agent branches)
and, per item, via `backend/detail_scraper.py:149` and `:159`. Those calls pass
`hdencode=False`, so `require_transport_authorization()` is skipped and the
construction proceeds freely.

**This path was confirmed by inspection only.** Limits worth stating plainly:

- The flake was never reproduced in this worktree. It did not fail in any run
  performed here.
- ~~No foreign thread has been observed actually constructing.~~ **Superseded —
  it has now been observed, 3/3 runs. See the next section.**

The independent attestation for the fix itself is the other chat's mutation
test, which reproduced the flake mechanism by injecting a foreign construction.

### Egress and construction: now observed, 4 runs

**Provenance: all figures below measured 2026-08-01 in this session**, in a
throwaway `scanhound:latest` container at repo state `7cc5275` (`main`), subset
`-k 'full_disc or policy or scanner or rss or feed'`. One exploratory run plus
three consecutive confirmation runs. Nothing here is quoted from a prior note.

A combined probe recorded (a) every outbound connect/DNS attempt, **blocking**
non-loopback ones so no traffic left the host, and (b) every
`cloudscraper.create_scraper` call with its calling thread and the test active
at the time.

Per run, identically in all three confirmation runs:

```
egress attempts (blocked): 2
create_scraper calls: 2 total, 2 off-main-thread
```

The first of each pair always lands in
`tests/test_api_routes.py::TestScanner::test_scan_start`. **The second lands in
a different, unrelated test every run:**

| run | 2nd construction lands in                                       |
|-----|-----------------------------------------------------------------|
| 1   | `test_api_routes.py::TestScheduler::test_trigger_no_scanner`      |
| 2   | `test_background_scanner.py::…::test_upsert_preserves_first_seen_category` |
| 3   | `test_background_scanner.py::…::test_scan_once_skips_purge_when_source_early_stopped` |
| (exploratory) | 2nd *egress* landed in `…::test_purge_removes_old_rows` |

**This is the flake, directly observed.** A thread leaked by `test_scan_start`
constructs a scraper while an unrelated test — often in a different file — is
the one running. `transport.cloudscraper.create_scraper` is patched
process-globally, so whichever test happens to own that moment absorbs the
construction. The landing point drifts across a span of tests from run to run,
which is exactly why the failure was intermittent and why it never reproduced in
isolation.

Run 2 is the clearest single piece of evidence: the construction landed in
`test_upsert_preserves_first_seen_category` while the DNS attempt that followed
it landed in `test_upsert_sets_category_when_empty` — the *next* test. The
construction is a separately-timed event that can fall inside a test boundary on
its own, which is precisely what the flaky assertion was catching.

Also corrected: the network I/O is done by `asyncio_0`, **not** by `_run_scan`
itself. `_run_scan` drives an event loop whose `run_in_executor(None, ...)` work
runs on the asyncio default executor, whose threads Python 3.12 names
`asyncio_N`. `_run_scan` is the owner; `asyncio_0` holds the socket. Both are in
the leaked set.

### Two claims from the first draft of this document that did not survive

1. *"The orphaned threads go on doing real scraping for the rest of the
   session."* **Not supported.** Under blocking, the observed behaviour is
   exactly two construct-then-connect events per session, not continuous
   activity. Two events are sufficient to explain a ~20% flake rate given the
   drift shown above, so the conclusion holds — but the mechanism is narrower
   than "continuous scraping" and should not be described that way.
2. *"78s blocked vs 127s unblocked, ~49s attributable to network wait."*
   **Withdrawn — confounded.** The 127s run was the first run in a freshly
   populated container; the blocked runs were warm. Warm-up is visible within
   the confirmation set itself (82s, 76s, 76s). The comparison is not
   apples-to-apples and cannot carry the conclusion I hung on it.

**Consequently the true unblocked egress volume remains unmeasured.** Measuring
it requires record-and-allow, which sends real requests to `hdencode.org` from
whatever network the suite runs on, and is Jesse's call — not something to run
casually. What *is* established is that egress is attempted, reproducibly, and
that construction leaks across test boundaries.

### How these numbers may and may not be labelled

Agreed wording after peer round (2026-08-02). Blocking egress does not hide
constructions — the assertion fires on `create_scraper()`, not on HTTP
completion — but it *truncates* the stream: the listing fetch fails at DNS, so
the per-item construction sites at `detail_scraper.py:149`/`:159` are never
reached, and the whole scan's timing is compressed.

An earlier draft called the blocked result a *lower bound on collision
probability*. **That is withdrawn as too strong.** Collision probability is not
monotonic in event count — unblocking adds events but also moves them, and can
move them *out* of the vulnerable window as easily as into it.

Defensible:

> Two foreign construction events were repeatedly observed under denied egress,
> one drifting across unrelated tests. That mechanism is capable of causing the
> historical failure. This is a minimum observed construction stream under
> denied egress, and ownership evidence — not rate evidence. The unblocked
> event count and timing distribution remain unmeasured.

Not defensible: that the historical 2-in-10 rate has been quantitatively
explained.

A middle measurement exists before any real traffic: drive the full
listing→detail path against a local fixture server or a fake transport
returning recorded HTML, with controllable latency. That reaches the detail
construction sites without contacting `hdencode.org` and gives a far better
collision envelope than denied egress.

---

## What is still open

The leak itself, queued as *"Stop app lifespan leaking background threads."*
Deliberately not attempted by either chat.

Files are clear of the other chat's work:

- `tests/conftest.py`
- `tests/test_api_routes.py`
- `backend/api/routes/scanner.py`

### The queued item is probably missing this

`_run_scan` is a different problem from the other eight and needs a different
fix.

The eight lifespan-owned threads survive a shutdown that *does* run — the
fixture uses `with TestClient(app)`, so the lifespan exits; the daemon threads
simply outlive it. Fix shape: join with timeout at shutdown.

`_run_scan` is **not lifespan-owned at all.** It is spawned per
`POST /scan/start` into a module-global `_scan_thread` at
`backend/api/routes/scanner.py:367`, and nothing holds a reference the lifespan
can reach. Adding timeouts to lifespan joins will not touch it. It also runs the
actual page crawl, making it the strongest candidate for real egress.

---

## Peer review outcome (ChatGPT, 2026-08-02) — conditional approval

Reviewed against `main`/`7cc5275` and `6d067e2`. Every checkable code claim in
the review was independently verified in this worktree before being accepted:

| claim | verified at |
|---|---|
| autouse fixture resets `_scan_state`, never joins `_scan_thread` | `tests/test_api_routes.py:73` |
| new app/lifespan per test | `tests/test_api_routes.py:93` |
| `_scan_slot` is per-`ScannerService` | `backend/scanner_service.py:219` |
| `_run_scan` late-derefs `reg.scanner` in-thread | `backend/api/routes/scanner.py:141` |
| loop closed without `shutdown_default_executor()` | `backend/scanner_service.py:366-374` |
| registry generation API already exists | `backend/api/dependencies.py:188,201,207` |
| 8s deadline < container stop grace | no `stop_grace_period` in `docker-compose.yml` → Docker default 10s |

**The five-leak/two-event gap is resolved**, and not in this document's favour.
The route's 409 guard is not being disproved — the autouse fixture resets
`_scan_state` to `idle` before every test without stopping the prior scan's
owner, and because `_scan_slot` is instance-level, a leaked scan holding the
previous fixture's scanner lock cannot block the next fixture's new scanner.
Both gates therefore pass and all five `test_scan_start*` calls legitimately
return 200. Orphans then see the old scanner, `None` (immediate return at
`scanner.py:142`), or a *later* test's scanner, depending on scheduling — which
is also why foreign work surfaces inside unrelated tests.

**New defect found by the review, not by either probe:** `run_scan` builds an
event loop and closes it without awaiting `loop.shutdown_default_executor()`.
Executor work can outlive the loop, so joining `_run_scan` alone would not have
been sufficient.

**The falsifiable prediction in this document is narrowed.** `asyncio_0` is the
name *every* asyncio loop gives its first default-executor worker, so it does
not identify an owner. Superseded wording:

> Fixing only lifespan-owned workers will not remove any transport construction
> proven to originate from foreground `_run_scan` work.

Supporting but not conclusive: `background-scanner` did not appear in the
surviving-thread set in any run here, including during the
`test_background_scanner.py` tests where the second event lands. Settling it
requires per-scan attribution (scan UUID, lifespan generation, captured
`id(scanner)`, and a scan-specific executor `thread_name_prefix`), which is
runnable under blocked egress.

**Sequencing changed on review:** `_run_scan` + executor ownership moves *ahead
of* the generic lifespan cleanup, because it is the demonstrated route leak, it
performs real external I/O, it corrupts global `_scan_state` across fixtures,
and the single `_scan_thread` handle loses earlier owners. The design shape is
**cooperative cancellation as the mechanism, bounded join as the proof** —
neither alone is sufficient. One overall shutdown deadline (8s production, 2–3s
tests), not a per-thread timeout multiplied out.

Also flagged: `tests/test_api_routes.py`'s reset fixture manufactures a state the
production route is designed to forbid. It should stop and join active scans
first, then reset — test cleanup should exercise the ownership contract, not
overwrite it.

## Guidance, in recommended order

**1. ~~Confirm egress first.~~ DONE — see "Egress: now observed" above.**
Confirmed: leaked threads attempt to reach `hdencode.org` during unrelated
tests. This is no longer test hygiene; the suite has an external footprint, and
anyone running it is sending traffic to a third-party site from whatever network
they are on. That raises the priority of step 2 specifically.

**2. Contain it test-side before touching the app.** `tests/conftest.py` has no
network mock; that is the root enabler and it is fixable with no production
risk. An autouse fixture that refuses outbound sockets would have caught this
entire class of bug. Expect it to surface other tests currently making live
calls — that is the point, but it means landing it as its own change with its
own failure triage, not bundled with the shutdown work.

A working prototype is left at `tests/tools/netwatch.py` (uncommitted, mirrors
the existing `tests/tools/threadleak.py` convention). Run with `-p netwatch`.
It patches `socket.socket.connect` and `socket.getaddrinfo`, allows loopback,
records `(host, port, thread, active test)` for everything else, and raises. To
turn it into the real guard, the raise needs to become a test failure attributed
to the *offending test* rather than an `OSError` the app's broad
`except Exception` handlers swallow — under the probe, both blocked attempts
were caught and logged by the app, and the suite still reported 296 passed.
That swallowing is itself worth noting: the suite cannot currently fail on
unexpected egress even when it happens.

**3. Then the lifespan joins.** Every join needs a timeout and a logged warning
on expiry, because a worker blocked in a long network call will otherwise hang
app shutdown. That is the real risk in this item and why it was left alone. Each
worker also needs a stop-event check it actually reaches — a join timeout on a
thread sleeping 30s uninterrupted (`poster-backfill`) just relocates the stall.

**4. Then `_run_scan` ownership.** `/scan/stop` already sets
`scanner.stop_scan_flag`, so the signalling half exists; what is missing is a
handle shutdown can reach and a bounded join.

Steps 1 and 2 are test-only and low risk. Steps 3 and 4 change production
shutdown behaviour and deserve separate review.

---

## Exact commands, results, and tree state

Everything below was run in a throwaway container, code `docker cp`-ed in (never
over the Windows 9p bind mount). Probe source is on this branch at
`tests/tools/probe.py` and `tests/tools/netwatch.py`.

**Command** (identical for all four runs; `-p probe` is the combined
blocked-egress + construction-attribution plugin):

```bash
python -m pytest tests/ -k 'full_disc or policy or scanner or rss or feed' -q --no-header -p no:cacheprovider -p probe -s
```

**Results:**

| run | wall | pytest result | egress attempts | create_scraper (off-main) |
|---|---|---|---|---|
| exploratory | — | 296 passed, 4 skipped, 3902 deselected | 2 | not instrumented |
| 1 | 82s | 296 passed, 4 skipped, 3902 deselected | 2 | 2 |
| 2 | 76s | 296 passed, 4 skipped, 3902 deselected | 2 | 2 |
| 3 | 76s | 296 passed, 4 skipped, 3902 deselected | 2 | 2 |

**Exit code:** 0 on all runs (wrapped in `timeout 900`, which did not fire).

**Baseline without the probe:** 296 passed, 4 skipped, 3902 deselected in 127s.
That 127s figure is the cold first run in a fresh container and must **not** be
compared against the warm probe runs — see the withdrawn-claims section above.

**Mutation results:** none of my own. The mutation evidence for the closed flake
fix is the other agent's, recorded in the commit message of `6d067e2` /
`9b059c5` (baseline PASS, injected foreign construction PASS, real regression
FAIL). I did not re-run it.

**CI:** none. There is no CI on the agent branches, so nothing here is
machine-attested; every result above is a local container run.

**Working tree:** clean at the reviewed SHA. All referenced probe code is
committed, not local-only.

**Not run:** the full suite. This subset is the reproduction scope; a full-suite
figure from a prior session (4392 passed / 4 skipped) is recorded in project
notes but was **not** re-measured here and should not be cited from this
document.

## Container recipe

This cost both chats time today.

```bash
docker run -d --rm --name sh-test-<purpose> --entrypoint sleep scanhound:latest infinity
```

- `pip install pytest pytest-asyncio httpx` — not in the prod image.
- `docker cp` in: `backend/`, `tests/`, `pytest.ini`, `scripts/`, `docs/`,
  `frontend/src`.
- Run with `docker exec -e PYTHONPATH=/work -e HOME=/tmp -w /work ... pytest`.
- `docker stop sh-test-<purpose>` — `--rm` makes stop equivalent to remove.

`pytest.ini` is **not** in the image and sets `asyncio_mode = auto`. Without it
pytest runs strict and async tests fail with *"async def functions are not
natively supported"* — which looks exactly like a code defect and is not one.
`scripts/`, `docs/` and `frontend/src` are read by several test files.

On Git Bash, prefix `docker exec` lines with `MSYS_NO_PATHCONV=1` or `/work`
gets mangled into a Windows path.
