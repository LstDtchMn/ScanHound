# Phase 1 — No-Egress Enforcement Gate: red-before evidence

**Date:** 2026-08-02
**Repository:** `LstDtchMn/ScanHound`
**Branch:** `claude/nice-meitner-2b717b`
**Scope:** test-only. No production file is touched by this phase.

Phase 1 of the plan of record in
`2026-08-02-thread-leak-lifespan-shutdown-chatgpt-response-7.md`.

---

## What changed

| file | role |
|---|---|
| `tests/tools/netwatch.py` | **enforcement.** Blocks non-allowlisted egress, records a thread-safe ledger, and forces a non-zero pytest exit status. |
| `tests/tools/netwatch_selftest.py` | **mutation proof.** Three cases proving the gate fires and is not unconditionally red. |
| `tests/tools/probe.py` | unchanged — remains the non-failing diagnostic. |

The split is deliberate: `probe.py` answers *what is happening*, `netwatch.py`
answers *this run is not acceptable*.

## Why the exit status is forced rather than raised

The application catches broad `Exception` around its network calls. An
`OSError` raised at the socket boundary is swallowed, and the run still reports
all tests passing. That is precisely how this leak stayed invisible.

`netwatch.py` therefore raises `UnauthorizedEgress(OSError)` to *stop* the call
— so application code behaves as it would against a real network failure — but
fails the run from `pytest_sessionfinish` by setting `session.exitstatus`, out
of band from any assertion.

## Evidence

All runs in a throwaway `scanhound:latest` container, code `docker cp`-ed in.
Exit codes captured directly from pytest, not through a pipe.

### Mutation proof

```bash
python -m pytest tests/tools/netwatch_selftest.py::<case> -q --no-header -p no:cacheprovider -p netwatch
```

| case | expectation | pytest result | **exit** |
|---|---|---|---|
| `test_no_egress` | guard green | 1 passed | **0** ✅ |
| `test_swallowed_egress` | guard red | 1 passed | **1** ✅ |
| `test_direct_egress` | guard red | 1 passed | **1** ✅ |

The control matters as much as the mutations: without it, a gate that always
returned non-zero would look identical to a working one.

`test_swallowed_egress` is the case that counts. It reproduces the application's
own pattern — connect on a background thread, catch broad `Exception`, assert
nothing. **It reports "1 passed" and still exits 1.** Under a guard that relied
on the raised exception, this would have been green.

### Red-before baseline

```bash
python -m pytest tests/ -k 'full_disc or policy or scanner or rss or feed' -q --no-header -p no:cacheprovider -p netwatch
```

```
EXIT=1
296 passed, 4 skipped, 3902 deselected, 13 warnings in 80.70s

netwatch: 2 UNAUTHORIZED EGRESS ATTEMPT(S)
      2  hdencode.org

  dns  hdencode.org:443
      thread:          asyncio_0
      observed during: tests/test_api_routes.py::TestScanner::test_scan_start
      originating op:  None (Phase 2)
  dns  hdencode.org:443
      thread:          asyncio_0
      observed during: tests/test_api_routes.py::TestScheduler::test_trigger_no_scanner
      originating op:  None (Phase 2)
```

**Every test passes and the run fails.** That is the gate working: the suite's
own assertions cannot see this, and now they do not need to.

Consistent with the four earlier probe runs — two attempts, `asyncio_0`, second
one drifting (here it landed in `TestScheduler::test_trigger_no_scanner`, as in
probe run 1).

## Design notes

- **Allowlist:** loopback, `::1`, `localhost`, `0.0.0.0`, and non-inet (AF_UNIX)
  addresses are always permitted. Extra hosts may be declared via
  `SCANHOUND_NETWATCH_ALLOW` (comma-separated) for a local fixture server.
  `hdencode.org` is deliberately **not** allowlisted — doing so to make the
  suite green would encode the defect.
- **Patched:** `socket.socket.connect`, `socket.socket.connect_ex`,
  `socket.create_connection`, `socket.getaddrinfo`. Installed in
  `pytest_configure`.
- **Between-test detection:** the ledger records
  `<between tests, after {nodeid}>` when an attempt lands outside a test body,
  so a worker that outlives its test is still attributed to a position in the
  run.
- **`observed_during_test` is not the culprit.** It is where the attempt landed.
  `originating_operation` is reserved for Phase 2's scan context and stays
  `None` until then. The terminal summary says so on every failing run, because
  the obvious misreading is to blame the observed test.

## Limits

- Local container runs only. There is no CI on this branch; nothing here is
  machine-attested.
- The gate proves *attempted* egress. It does not measure what unblocked egress
  would do — that remains unmeasured and needs record-and-allow.
- Not run against the full suite. Other tests outside this subset may attempt
  egress and would now fail the run; that triage is its own task.
- The gate cannot yet name which scan leaked a worker. That is Phase 2.
