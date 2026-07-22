# PR #21 adversarial review — metadata-scan cancellation

**Reviewer:** Claude (independent read of the code before trusting any
self-reported test results; per Jesse's authorization to take ownership of
this workstream by reading the code first).
**Reviewed ref:** `fix/metadata-scan-cancellation` @ `1fd45921199d5cf4ce4e7177ab9f6f2eaac3e02e`
**Base:** `main` @ `f83872469010e544ec0ec79e4eb6e0bc053600dd`
**Verdict: DO NOT MERGE YET.** One confirmed, empirically-reproduced defect
(latency-unbounded cancellation under a specific real-world condition) and a
confirmed test-coverage gap that let it ship undetected. Everything else
checked out correctly — the safety-critical invariant (cancellation never
becomes an authoritative negative) is properly implemented.

No merge, deploy, or production action has been taken. This is a review
artifact only, pushed to its own branch off `main`.

---

## 1. Scope and methodology

Read the full diff (`git diff f838724..1fd4592`, 320 insertions / 29
deletions across 8 files) and the complete final state of every changed
function (not diff hunks alone) via `git show <ref>:<path>`, since the
reviewing checkout was on an unrelated branch. Ran the PR's own test scope
and a broader regression in a throwaway container. Wrote and ran new tests
against **real OS child processes** — the PR's own tests mock
`subprocess.Popen` exclusively (see §4) — to empirically verify claims that
can't be settled by reading code alone: does termination actually work
against a real process, is the kill-escalation path reachable, is latency
actually bounded.

## 2. Confirmed defect: cancellation latency is unbounded when a probed
   tool's descendant process outlives the direct child

**Mechanism.** `backend/rename/process_control.py::_stop()`:

```python
def _stop(process) -> None:
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()          # <-- no timeout
```

The tracked child is created with `stdout=subprocess.PIPE,
stderr=subprocess.PIPE`. If that child itself spawns a subprocess **without
redirecting the subprocess's own stdout/stderr** (the Python/OS default is to
inherit the parent's file descriptors), the descendant holds its own copy of
the pipe's write end. Terminating and killing the *direct* child does not
touch the descendant. `communicate()` cannot observe EOF on a pipe until
every process holding its write end closes it — so the final, **unbounded**
`communicate()` call blocks until the descendant exits on its own, for as
long as that takes.

**Empirical reproduction** (not theoretical — measured):

| Descendant lifetime | Measured cancellation latency |
|---|---|
| 8 s | **8.03 s** |
| 30 s | **30.62 s** |

Both runs matched the descendant's sleep duration almost exactly, confirming
the mechanism precisely (not a coincidental slow test). Evidence:
[`pr21-review-evidence/test_process_control_real_child.py`](pr21-review-evidence/test_process_control_real_child.py),
`test_descendant_process_survives_direct_child_kill_general_principle` and the
isolated 8-second repro captured in this review's own session log.

**Why this matters.** The entire point of PR #21 is a bounded-cancellation
guarantee (the incident it fixes was a 20+ minute unbounded wait on
`dovi_tool`). This defect reintroduces an unbounded wait under a specific,
plausible condition — differing only in that the *unbounded* window now
starts after termination/kill of the tracked process, rather than before it.

**Applicability to the real tools — UNVERIFIED, disclosed explicitly.** I do
not have `dovi_tool` or `hdr10plus_tool` installed in this review environment
and cannot inspect their actual process trees. Based on general knowledge,
both are commonly distributed as self-contained Rust CLI binaries with their
own container demuxers, which argues against them shelling out to a helper
process such as `ffmpeg` — but this is an assessment, not a verified fact.
The review handoff explicitly asked this to be settled "from executable
evidence" against the real binaries; that evidence does not exist in this
environment. **This is the one open question a reviewer with access to the
real tools/production host should settle before deciding how urgently to
fix this.**

**Recommended fix (minimal, low-risk).** Bound the final `communicate()`
call too and treat a repeat timeout as acceptable — by that point the
*tracked* child is confirmed dead (`kill()` succeeded on it), and the caller
never uses `_stop()`'s return value (the outer loop unconditionally raises
`ProcessCancelled` immediately after calling it):

```python
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass  # a descendant may still hold the pipe open; nothing more to do
```

**More thorough fix (if the real tools are ever confirmed to spawn
descendants).** Run the probed tool in its own process group
(`start_new_session=True` on POSIX, `CREATE_NEW_PROCESS_GROUP` on Windows)
and terminate/kill the whole group (`os.killpg` / a Windows job object).
This actually stops the descendant rather than merely avoiding the hang
while it as an orphan continues running. The original handoff explicitly
cautioned against adding this complexity "without reproducing a real
descendant-process problem" — that reproduction now exists (§2 above), so
the decision to add it or not can be made on real evidence rather than
speculation.

## 3. Minor: `process.kill()` is not exception-guarded

In the same `_stop()` function, `process.terminate()` is wrapped in
`try/except OSError: pass`, but the subsequent `process.kill()` is not. In
the narrow window where the process dies between the `TimeoutExpired` and
the `kill()` call, `kill()` can raise `ProcessLookupError` (an `OSError`
subclass) uncaught, propagating out of `run_cancellable()` as an unexpected
exception instead of a clean `ProcessCancelled`. Low probability, trivial
fix: wrap `kill()` the same way `terminate()` is wrapped.

## 4. Confirmed test-coverage gap: no test exercises a real OS process

Every new test across `tests/test_dv_detect.py`,
`tests/test_hdr10plus_detect.py`, and `tests/test_metadata_scan_runs.py`
replaces `subprocess.Popen` with a hand-written fake `Process` class. None
spawn a real child process; none simulate a process that ignores `SIGTERM`
(so the `kill()`-escalation path in `_stop()` is not exercised by any test in
this PR); there is no dedicated `tests/test_process_control.py` for the new
primitive at all.

This is precisely what the review handoff explicitly required and explicitly
warned against skipping: *"Avoid mocks that prove only the mock's behavior...
Include at least one real, controllable child process."* The gap is not
academic — it is exactly what let the defect in §2 ship without detection;
every existing mock's `terminate()` unconditionally "succeeds" on the first
call, so the code path that would have revealed the hang was never run.

`docs/feature-pack-review/pr21-review-evidence/test_process_control_real_child.py`
(new, written for this review, not part of the PR) closes this gap: 5 tests
against real `python -c ...` child processes, run on Linux (the production
platform) inside the project's throwaway `sh-test` container:

```
tests/test_process_control_real_child.py::test_kill_escalation_against_a_real_stubborn_child PASSED
tests/test_process_control_real_child.py::test_real_child_actually_terminates_not_just_marked_cancelled PASSED
tests/test_process_control_real_child.py::test_cancellation_latency_is_bounded_for_a_cooperative_child PASSED
tests/test_process_control_real_child.py::test_uncancelled_real_child_returns_real_stdout_stderr PASSED
tests/test_process_control_real_child.py::test_descendant_process_survives_direct_child_kill_general_principle PASSED (30.62s -- see §2)
```

The first four **positively confirm** the primitive is correct against real
processes: a genuinely SIGTERM-ignoring child is actually killed (proven via
`os.kill(pid, 0)` raising `ProcessLookupError` — the OS process is truly
gone, not merely that our code stopped waiting for it), within a bounded
~6-8 second window for the cooperative/stubborn-but-killable cases. This
substantially raises confidence in the core design; the defect in §2 is real
but narrow, not evidence the whole approach is unsound.

## 5. Confirmed correct: cancellation never becomes an authoritative negative

Read every exit path in `dv_detect.py::detect_layer` and
`hdr10plus_detect.py` (`_quick_frame_evidence`, `_tool_version`,
`_full_extract`, `detect_hdr10plus`), and the orchestrator
(`plex_metadata_scan.py::_process_file`). `ProcessCancelled` and
`subprocess.TimeoutExpired` map to the *identical* observable outcome in
every caller — `layer/state = unknown`, `error = "cancelled"` or `"timeout"`
— never a negative (`LAYER_NONE` / `state="absent"`). Because both exceptions
produce equivalent outcomes, the theoretical timeout/cancellation race named
in the original review handoff (failure mode #6) does not matter in
practice: whichever one fires, the result is the same safe "unknown,
retryable" state.

The orchestrator correctly distinguishes cancellation from failure at the
counter level too: `plex_metadata_scan.py::_process_file` checks
`dv_result.get("error") == "cancelled"` and
`specs.get("hdr10plus_evidence", {}).get("error") == "cancelled"`
(verified the `"hdr10plus_evidence"` key name against the real
`probe_detailed()` body — it matches exactly what the check expects) and,
when true, sets `count_outcome = False` and leaves the manifest item
`status="pending"` rather than incrementing `processed`/`succeeded`/`failed`.
This is precisely the required "keep the interrupted manifest item pending
and retryable" and "do not count cancellation as success or failure"
behavior.

## 6. Confirmed correct: temp file cleanup on every exit path

- `dv_detect.py::detect_layer` — the RPU temp file is removed in a `finally`
  block covering the entire try/except chain (normal return, `ProcessCancelled`,
  `TimeoutExpired`, or any other exception). This `finally` block predates
  PR #21 and was correctly left untouched.
- `hdr10plus_detect.py::_full_extract` — uses `tempfile.TemporaryDirectory()`
  as a context manager, which Python guarantees to clean up on any exit path
  including an exception raised inside the `with` block.

## 7. Disclosed scope limitation, not a defect

`mediainfo.py::probe_detailed` calls `_scan_stream_details(path, timeout)`
**before** the HDR10+/DV branches, without forwarding `cancel_requested` —
that stage is not cancellable. It remains bounded by the same `timeout`
parameter (30s by default), so the worst-case added wait from this gap is on
the order of 30 seconds, not the 20+ minutes the original incident involved.
Likely an intentional, reasonable scope limit (the incident was specifically
about `dovi_tool`/`hdr10plus_tool`), but should be stated explicitly rather
than left implicit.

## 8. Test results

| Suite | Result |
|---|---|
| PR's own claimed changed-file scope (3 files) | **32 / 32 passed** |
| Broader DV/HDR/inventory sweep (19 files) | 197 passed, 11 failed |
| Same 11 tests, run against **base** `f838724` (pre-PR #21) | **Identical 11 failures, same test names** |
| New real-subprocess suite (this review, 5 tests) | **5 / 5 passed** (one intentionally slow — see §2) |

The 11 failures are **not attributable to PR #21** — proven by running the
identical test selection against the base commit and getting the exact same
failing test names. All 11 are `FileNotFoundError`s in `test_dv_host_scan.py`
(a host-filesystem-path-dependent script test) and two doc/config-presence
checks in `test_metadata_scan_runbook.py`; neither file is in PR #21's
changed-files list. Classification: **environment limitation** (this
review's minimal container snapshot lacks the host paths and `docs/`
runbook file those specific tests expect), not a product or test defect in
this PR.

## 9. Summary table

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Unbounded cancellation latency when a descendant inherits the pipe FD | **High**, real-world trigger unverified | Confirmed, reproduced, fix proposed |
| 2 | `process.kill()` not exception-guarded | Low | Confirmed, trivial fix proposed |
| 3 | No test exercises a real OS process / no `test_process_control.py` | Confirmed gap | Closed by this review's new test file |
| 4 | Cancellation never produces an authoritative negative | — | **Verified correct** |
| 5 | Temp file cleanup on every exit path | — | **Verified correct** |
| 6 | `_scan_stream_details` not cancellable | Low, bounded to ~30s | Disclosed scope limitation |
| 7 | 11 pre-existing test failures in broader sweep | — | Confirmed **not** attributable to this PR |

## 10. Recommendation

Do not merge as-is. Findings #1 and #2 are both small, well-understood code
changes (see §2 and §3 for exact proposed diffs) — fixing both and adding
this review's real-process test file (or an equivalent) to the PR should be
enough to merge with confidence. Finding #1's real-world severity still
depends on the open question about the actual tool binaries' process
behavior (§2), which this environment cannot settle; whoever has access to
the production host's installed `dovi_tool`/`hdr10plus_tool` should check
`--help`/source/strace evidence before deciding whether the minimal fix is
sufficient or the process-group approach is warranted.
