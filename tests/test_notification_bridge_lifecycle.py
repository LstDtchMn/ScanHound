"""The notification loop thread must retire its own loop, executor included.

Nothing outside the loop thread ever called ``loop.close()`` on it, so the
default executor populated by the desktop-toast path's
``run_in_executor(None, ...)`` was never retired and its ``asyncio_N`` workers
accumulated one lifespan after another.

Version-independent by construction: the drain uses no 3.12-only argument, so
these assertions mean the same thing on 3.11 and 3.12.
"""
import asyncio
import threading
import time

from backend.notification_bridge import NotificationBridge


def _bridge_with_running_loop():
    """A bridge whose loop thread is up, without configuring real channels."""
    bridge = NotificationBridge()
    bridge._start_loop()
    assert bridge._ready.wait(timeout=5), "loop never signalled ready"
    return bridge


def test_loop_thread_exits_and_closes_the_loop():
    bridge = _bridge_with_running_loop()
    loop, thread = bridge._loop, bridge._thread

    bridge.shutdown()

    assert not thread.is_alive(), "loop thread outlived shutdown()"
    assert loop.is_closed(), "loop was stopped but never closed"


def test_the_loops_executor_threads_are_retired_by_shutdown():
    """The actual leak: a toast dispatched via run_in_executor left a worker."""
    bridge = _bridge_with_running_loop()
    seen = {}

    def _toast():
        seen["thread"] = threading.current_thread()

    # Mirror how NotificationChannel dispatches a desktop toast.
    # run_in_executor returns a Future, not a coroutine, so it has to be
    # awaited from inside one to cross into the loop thread.
    async def _dispatch():
        await bridge._loop.run_in_executor(None, _toast)

    asyncio.run_coroutine_threadsafe(_dispatch(), bridge._loop).result(timeout=5)

    worker = seen["thread"]
    assert worker.is_alive(), "executor worker should exist before shutdown"

    bridge.shutdown()

    worker.join(timeout=5)
    assert not worker.is_alive(), (
        "executor worker survived shutdown — this is the asyncio_N leak")


def test_shutdown_is_idempotent_and_bounded():
    """Teardown paths call this more than once; the second must not hang."""
    bridge = _bridge_with_running_loop()

    began = time.monotonic()
    bridge.shutdown()
    bridge.shutdown()
    elapsed = time.monotonic() - began

    # Two full 2s joins would be 4s; a clean stop is near-instant.
    assert elapsed < 2.0, f"repeated shutdown took {elapsed:.1f}s"


def test_shutdown_without_a_loop_is_a_no_op():
    """configure() bails early when NotificationManager is unavailable."""
    NotificationBridge().shutdown()  # must not raise


def test_a_wedged_drain_strands_the_thread_rather_than_shutdown():
    """The bound that matters: a stuck toast must not hold shutdown open.

    The drain runs on the loop thread deliberately, so shutdown()'s join is
    what bounds it. Here the executor worker never returns; shutdown must
    still come back promptly and simply leave that thread behind.
    """
    bridge = _bridge_with_running_loop()
    release = threading.Event()
    entered = threading.Event()

    def _wedged_toast():
        entered.set()
        release.wait(timeout=30)

    async def _dispatch_wedged():
        await bridge._loop.run_in_executor(None, _wedged_toast)

    asyncio.run_coroutine_threadsafe(_dispatch_wedged(), bridge._loop)
    assert entered.wait(timeout=5), "toast never started"
    stranded = bridge._thread   # shutdown() clears _loop, not _thread

    try:
        began = time.monotonic()
        bridge.shutdown()
        elapsed = time.monotonic() - began
        # Bounded by the 2s join, not by the wedged worker's 30s.
        assert elapsed < 4.0, f"shutdown blocked for {elapsed:.1f}s"
        assert stranded.is_alive(), "expected the loop thread to be stranded"
    finally:
        # Let it finish, so this test does not itself leak the thread it
        # is asserting gets stranded — that would show up as a real leak
        # in the suite-wide threadleak report and mask a genuine one.
        release.set()
        stranded.join(timeout=10)


def test_pending_non_executor_tasks_are_cancelled_not_destroyed():
    """send() is fire-and-forget, so a task can still be awaiting at close.

    Closing the loop with tasks pending destroys them mid-await ("Task was
    destroyed but it is pending!") and silently loses the notification. The
    owner thread must cancel and gather first, the way asyncio.run() does.
    """
    bridge = _bridge_with_running_loop()
    started = threading.Event()
    outcome = {}

    async def _slow_notification():
        started.set()
        try:
            await asyncio.sleep(300)      # a webhook POST / batch delay
        except asyncio.CancelledError:
            outcome["cancelled"] = True
            raise

    handle = asyncio.run_coroutine_threadsafe(_slow_notification(), bridge._loop)
    assert started.wait(timeout=5), "task never started"
    loop = bridge._loop

    bridge.shutdown()

    assert loop.is_closed(), "loop should be closed"
    assert outcome.get("cancelled"), (
        "pending task was destroyed rather than cancelled and gathered")
    assert handle.cancelled() or handle.done(), "task future left unresolved"
