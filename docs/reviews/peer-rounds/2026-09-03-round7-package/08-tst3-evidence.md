# TST-3 evidence — 2026-09-05

Worktree `C:\Users\NLSur\AppData\Local\Temp\tst3`, branch `fix/tst3-dv-host-scan-socket-abort` off `main` @ `0a2751d`. Head: 4e89f96. PR #115, draft, unmerged. Tests only: one file, `tests/test_dv_host_scan.py`.

## 1. The symptom, as recorded before diagnosis

Eight full-suite runs on the Windows host between 2026-09-03 and 2026-09-05; two failed, each once, on a different test in `tests/test_dv_host_scan.py`:

| date | test | message |
|---|---|---|
| 2026-09-03 | `test_post_rows_direct_success_delivers_key` | `dv-host-rows POST failed: [WinError 10053] An established connection was aborted by the software in your host machine` |
| 2026-09-04 | `test_post_rows_ignores_ambient_proxy` | same |

Each passed alone and with its file; Linux CI never showed it. The reviewer set the exit condition: name the socket owner, the lifecycle state at the abort, where the exception enters ScanHound, and why any containment cannot suppress a materially different failure; repeated instrumented runs, not one success.

## 2. Whose socket, in what state, entering where

- **Owner.** The test's own throwaway server: `_serve()` (`tests/test_dv_host_scan.py:592`) builds an `http.server.HTTPServer` on `127.0.0.1:0` and serves it from a daemon thread; each affected test defines a `BaseHTTPRequestHandler` subclass. The client is the product function under test, `_post_rows` in `scripts/host-detector/dv_host_scan.py:346`, which posts one JSON body with `urllib` through the no-redirect opener (`:376`).
- **Lifecycle state.** Active, inside the request; not shutdown or fixture teardown (`srv.shutdown()` runs only after `_post_rows` has returned). The handler answers the POST without ever calling `rfile.read(Content-Length)`, so it never waits for the request body; when `do_POST` returns, `socketserver` closes the connection. The client sends the headers and the body as two separate socket writes (`http.client` `_send_output`), so under load the body's segment can reach the server after the toy server has already answered and closed. A segment arriving on a closed socket draws a reset, and the client's read of the response fails with 10053.
- **The discriminating condition, measured.** The diagnosis lane's MSG_PEEK probe at the moment of close, across 1,180 requests: body bytes still unread in the server's receive buffer in 953 of them, and none of those failed; in all ten failures the probe read zero bytes, i.e. the body had not yet arrived when the server closed. So the harmful case is the late-arriving body, not merely unread bytes at close; the fix removes both, because reading `Content-Length` bytes blocks until the whole request has arrived and consumes it, so the close always follows the complete request and sends a plain FIN.
- **Where the exception enters ScanHound.** `_post_rows` catches it as `OSError` at `dv_host_scan.py:382-384`, logs `dv-host-rows POST failed: %s` and returns `False`; the test then asserts on that `False`. The supervisor's proof captured the raising frames on the one abort it produced: `http.client` `getresponse` → `begin` → `_read_status` → `socket.readinto`, so the client had finished sending and was reading the status line when the reset arrived. The product client is not at fault: it sends a well-formed request and treats the abort as a failed POST, which is the correct product behaviour. In production it posts to the FastAPI server, which reads request bodies; the toy server is the only place the race exists.
- **Why Windows only.** Linux CI never showed it in any run. The usual explanation is the stacks' different handling of a reset that arrives after the response: Windows discards already-received data and reports the abort on the next read, Linux delivers what was queued before reporting it. This is stated as the likely reason, not measured here; the fix does not depend on it.

## 3. Reproduced, repeatedly, under load

The diagnosis lane's stress run: `tests/test_dv_host_scan.py` twenty times in the combined copy that carries #110, with four CPU/IO load generators: 1 of 20 runs failed, iteration 9, `test_post_rows_direct_success_delivers_key`, the same message. The lane's tight loop (fresh server, one `_post_rows`, shutdown; a verbatim copy of the `Direct` handler; six load generators; ten-minute budget): 10 aborts in 1,180 attempts, every one with the probe reading zero unread bytes at close (body not yet arrived), as described above.

A note on faithfulness: the supervisor's first proof draft gave the unfixed handler a `Content-Length` response header and could not reproduce the abort in 200 attempts. The test's handlers send an HTTP/1.0 response with no `Content-Length`, so the client reads the body until end-of-stream, and that extra end-of-stream read is the one the reset overtakes. The proof below uses the handler's exact shape.

## 4. The fix

One helper, `_consume_body(handler)` (`tests/test_dv_host_scan.py:601`), reads exactly `Content-Length` bytes (tolerating a missing or zero header, so the shared GET/POST handler is safe); every POST handler in the file calls it first:

| handler | test | before |
|---|---|---|
| `Sink._h` (`do_GET = do_POST = _h`) | `test_post_rows_refuses_redirect_and_never_leaks_key` | replied 200 without reading |
| `Redirector.do_POST` | same test | replied 302 without reading |
| `Proxy.do_POST` | `test_post_rows_ignores_ambient_proxy` | replied 200 without reading |
| `Direct.do_POST` | same test | replied 200 with the reconciling body, without reading |
| `Direct.do_POST` | `test_post_rows_direct_success_delivers_key` | same |

Nothing the handlers assert or reply changed. The listening sockets are now also closed after `shutdown()` in the three `finally` blocks (hygiene: they were never closed, unlike `tests/test_clicknload_transport.py:55-56`; not part of the mechanism). No retry, rerun or quarantine was added. Diff: one file, 31 insertions, 4 deletions.

## 5. Shown both ways, at first hand

The supervisor's own proof (`tst3_proof_supervisor.py`): two handlers, identical except for the `_consume_body` call, interleaved unfixed/fixed on every iteration so both arms see the same load profile (six load generators); the client is the real `_post_rows`, imported the way the test imports it; each iteration starts a fresh server, posts once, shuts down.

| arm | aborts (10053) | attempts with body bytes unread at close |
|---|---|---|
| unfixed | 1 of 400 (attempt 223) | 298 of 400 |
| fixed | 0 of 400 | 0 of 400 |

The one abort: the exact message; the server's probe at close read 0 bytes (the body had not yet arrived); the exception was raised in `http.client` `getresponse` → `begin` → `_read_status` → `socket.readinto`, i.e. the client's read of the status line. Log: `tst3_proof_supervisor_run4.log` (406 s). The fix lane's independent, sequential proof (500 per arm, importing the real `_consume_body` from the patched file): unfixed 0 aborts, 370 of 500 with unread bytes at close; fixed 0 and 0. Two earlier supervisor runs were killed by a lane's exit-time process cleanup and are not counted; the first draft (with the `Content-Length` reply header, see section 3) is not counted either.

## 6. The test file under load, after the fix

`tests/test_dv_host_scan.py` ten times under four load generators in the fix worktree: 10 of 10 passed, 37 tests each, about 4.5 s per run (`tst3_file_loop_run1.log`). The lane's own loops on the same worktree: 15 of 15 under six generators, 10 of 10 under four. Before the fix, the same file under four generators failed 1 of 20 runs (section 3).

## 7. Why this cannot suppress a different failure

The change is confined to the toy servers' request handling: they now drain the request before answering. The assertions are unchanged; a redirect still fails the post and the key still never reaches the sink; the ambient proxy is still not hit; the direct success still delivers the key. A product regression in `_post_rows` (a 401, a redirect followed, a body that does not reconcile) surfaces exactly as before. There is no retry, no rerun on failure, no platform skip and no exception filter, so no failure of a different shape is absorbed.

## 8. Suites and CI

CI on `4e89f96` (ubuntu-latest, both the push and the pull-request runs): Tests workflow green on Python 3.11 (24 min) and 3.12 (12 min), frontend green. CI VERIFIED. Real trash root absent throughout (`Test-Path C:\.scanhound-trash` False). The worktree lacks #110, so only this test file was run in it; the diagnosis stress run used the combined copy that carries #110.

## 9. Process

Diagnosis (Sonnet lane): read the harness and the client, hypothesised the unread-body close, reproduced under load with instrumentation. Fix (Sonnet lane): the helper and five call sites. The supervisor reviewed the diff line by line, ran the both-ways proof and the file loop at first hand, and committed.
