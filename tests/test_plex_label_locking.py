"""Concurrent Plex label mutation must not lose a label.

`add_label`/`remove_label` are read-modify-write on a fetched Plex item. The
scheduled DV sync and the version-badge sync run sequentially on the maintenance
thread, but the MANUAL DV sync endpoint runs on its own daemon thread and can
overlap either. Two edits built from the same stale read mean one silently drops
the other's work (peer review 2026-08-19, M3).

The disjointness of VERSION_LABELS and dv_labeler.MANAGED proves neither
reconciler *intends* to remove the other's labels. It says nothing about whether
a concurrent write can do it by accident, which is what these cover.
"""
import threading
import time

from backend.plex_manager import PlexManager, _LABEL_LOCK_STRIPES


class _RaceyItem:
    """A Plex item whose label write is a read, a pause, then a write — the
    shape that loses data when two writers interleave."""

    def __init__(self):
        self.labels = []

    def addLabel(self, label):
        current = list(self.labels)   # read
        time.sleep(0.005)             # the window a real round trip opens
        self.labels = current + [label]  # write

    def removeLabel(self, label):
        current = list(self.labels)
        time.sleep(0.005)
        self.labels = [x for x in current if x != label]


def _manager(item):
    pm = PlexManager.__new__(PlexManager)
    pm._label_locks = [threading.Lock() for _ in range(_LABEL_LOCK_STRIPES)]

    class _Server:
        def fetchItem(self, _key):
            return item
    pm._server = _Server()
    return pm


def test_concurrent_writers_on_ONE_item_do_not_lose_a_label():
    """THE race. Without the lock both threads read `[]`, and whichever writes
    second overwrites the first — exactly how a DV badge would vanish when the
    version sync ran beside it."""
    item = _RaceyItem()
    pm = _manager(item)
    threads = [threading.Thread(target=pm.add_label, args=("1", lbl))
               for lbl in ("DV FEL", "2 Versions")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(item.labels) == ["2 Versions", "DV FEL"], (
        f"a concurrent write lost a label: {item.labels}")


def test_it_holds_in_BOTH_orders():
    """DV then version, and version then DV, as the review asked. Repeated so a
    lucky interleaving cannot pass by accident."""
    for _ in range(5):
        for first, second in (("DV FEL", "3 Versions"), ("3 Versions", "DV FEL")):
            item = _RaceyItem()
            pm = _manager(item)
            a = threading.Thread(target=pm.add_label, args=("7", first))
            b = threading.Thread(target=pm.add_label, args=("7", second))
            a.start(); b.start(); a.join(); b.join()
            assert sorted(item.labels) == sorted([first, second]), item.labels


def test_a_remove_cannot_clobber_a_concurrent_add():
    """The mixed case: one reconciler retiring its own stale label while the
    other adds a new one."""
    item = _RaceyItem()
    item.labels = ["DV MEL"]
    pm = _manager(item)
    a = threading.Thread(target=pm.remove_label, args=("3", "DV MEL"))
    b = threading.Thread(target=pm.add_label, args=("3", "2 Versions"))
    a.start(); b.start(); a.join(); b.join()
    assert item.labels == ["2 Versions"], item.labels


def test_different_items_are_not_serialised_into_one_queue():
    """The reason for striping rather than one global lock: unrelated titles
    must still write concurrently, or a 1,000-title backfill becomes a
    single-file queue.

    Asserted with a BARRIER rather than a stopwatch. A wall-clock bound is a
    flaky test on a loaded machine; a barrier that only releases when N writers
    are inside their critical sections at once proves the property outright, and
    fails deterministically (by timing out) if they are serialised.
    """
    import pytest

    inside = threading.Barrier(4, timeout=5)
    items = {}

    class _BarrierItem:
        def __init__(self):
            self.labels = []

        def addLabel(self, label):
            # Only completes if four writers are inside at the same time.
            inside.wait()
            self.labels.append(label)

    pm = PlexManager.__new__(PlexManager)
    pm._label_locks = [threading.Lock() for _ in range(_LABEL_LOCK_STRIPES)]

    class _Server:
        def fetchItem(self, key):
            return items.setdefault(key, _BarrierItem())
    pm._server = _Server()

    # Keys chosen so their stripes differ, which is what makes concurrency
    # possible at all; assert that rather than assume it.
    keys, seen = [], set()
    k = 1
    while len(keys) < 4:
        lock = pm._label_lock(str(k))
        if id(lock) not in seen:
            seen.add(id(lock))
            keys.append(str(k))
        k += 1

    errors = []

    def _write(key):
        try:
            pm.add_label(key, "2 Versions")
        except threading.BrokenBarrierError:
            errors.append(key)

    threads = [threading.Thread(target=_write, args=(key,)) for key in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, (
        "writers on different items could not be in their critical sections at "
        "once — the locking is serialising unrelated titles")
    assert all(items[int(key)].labels == ["2 Versions"] for key in keys)


def test_the_same_key_always_maps_to_the_same_stripe():
    pm = PlexManager.__new__(PlexManager)
    pm._label_locks = [threading.Lock() for _ in range(_LABEL_LOCK_STRIPES)]
    assert pm._label_lock("123") is pm._label_lock("123")
    assert pm._label_lock(123) is pm._label_lock("123"), "int and str keys must agree"
