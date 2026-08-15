"""Negative controls for dv_health_check: prove it DETECTS each failure.

notify() is never called here -- only the detection functions -- so no push is
sent. A check that has only ever been observed passing is not evidence of
anything.
"""
import sys
from pathlib import Path

sys.path.insert(0, r'X:/Docker Apps/ScanHound/scripts/host-detector')
import dv_health_check as H

fails = 0


def check(label, cond):
    global fails
    print(('  PASS  ' if cond else '  FAIL  ') + label)
    if not cond:
        fails += 1


print('1) healthy baseline (the positive control -- without it the rest is vacuous)')
check('tools report no problems', H.check_tools() == [])
p, s = H.check_db()
check('db reports no problems', p == [])
check('db stats are populated', s.get('total', 0) > 0)

print('2) missing tools directory must ALERT, not pass quietly')
orig = H.TOOLS
H.TOOLS = Path(r'C:/Tools-does-not-exist')
probs = H.check_tools()
check('missing dir produces a problem', len(probs) == 1 and 'does not exist' in probs[0])
H.TOOLS = orig

print('3) a missing binary must be named')
H.TOOLS = Path(r'C:/Windows')          # exists, but has none of our tools
probs = H.check_tools()
check('all three required tools reported missing', len(probs) == 3)
check('names the tool', any('dovi_tool.exe' in x for x in probs))
H.TOOLS = orig

print('4) unreadable database must ALERT, not pass quietly')
origdb = H.DB
H.DB = Path(r'X:/Docker Apps/ScanHound/data/definitely-not-here.db')
probs, stats = H.check_db()
check('missing db produces a problem', probs and 'missing' in probs[0])
check('no stats invented', stats == {})
H.DB = origdb

print('5) denial count above threshold must ALERT')
orig_thresh = H.DENIED_THRESHOLD
H.DENIED_THRESHOLD = -1               # every real count now exceeds it
probs, stats = H.check_db()
check('threshold breach is reported', any('Access is denied' in x for x in probs))
H.DENIED_THRESHOLD = orig_thresh
probs, _ = H.check_db()
check('restored threshold is quiet again', probs == [])

print('6) JDownloader poll liveness')
import io as _io
import json as _json
import urllib.request as _url


class _FakeResp:
    def __init__(self, payload):
        self._b = _json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _with_health(payload, exc=None):
    """Point check_jd at a synthetic /health body (or a failure)."""
    def fake(url, timeout=None):
        if exc:
            raise exc
        return _FakeResp(payload)
    orig = _url.urlopen
    _url.urlopen = fake
    try:
        return H.check_jd()
    finally:
        _url.urlopen = orig


# Healthy: recent success -> silent. Without this control the rest is vacuous,
# because a check_jd that always alerts would satisfy every negative case.
check('a fresh poll is silent', _with_health(
    {'jd_enabled': True, 'jd_poll': {'stalled_seconds': 12, 'consecutive_failures': 0}}) == [])

check('a long stall ALERTS', len(_with_health(
    {'jd_enabled': True,
     'jd_poll': {'stalled_seconds': H.JD_STALL_SECONDS + 1,
                 'consecutive_failures': 9, 'last_success_at': 'x'}})) == 1)

check('JD disabled stays silent', _with_health(
    {'jd_enabled': False, 'jd_poll': {'stalled_seconds': 99999}}) == [])

check('just-started (no success, no failures) stays silent', _with_health(
    {'jd_enabled': True,
     'jd_poll': {'stalled_seconds': None, 'consecutive_failures': 0}}) == [])

check('never-succeeded WHILE failing ALERTS', len(_with_health(
    {'jd_enabled': True,
     'jd_poll': {'stalled_seconds': None, 'consecutive_failures': 4}})) == 1)

check('an unreachable API ALERTS', len(_with_health(
    None, exc=OSError('connection refused'))) == 1)

check('an older build without jd_poll stays silent', _with_health(
    {'jd_enabled': True}) == [])

print()
print('7) alert state machine (main() lifecycle)')
# Peer review 2026-08-15: the old single global marker meant one subsystem's
# alert suppressed EVERY later one until all problems cleared. These drive
# main() itself -- the individual check_* tests above cannot see this.
sent = []


def _fake_notify(title, message, priority=8):
    sent.append(title)
    return True


def _drive(tools, dbp, jd, deliver=True):
    """Run main() with the three checks stubbed to the given problem lists."""
    del sent[:]
    o_tools, o_db, o_jd, o_notify = H.check_tools, H.check_db, H.check_jd, H.notify
    H.check_tools = lambda: list(tools)
    H.check_db = lambda: (list(dbp), {'classified': 1, 'total': 2, 'denied': 0})
    H.check_jd = lambda: list(jd)
    H.notify = _fake_notify if deliver else (lambda *a, **k: False)
    try:
        H.main()
    finally:
        H.check_tools, H.check_db, H.check_jd, H.notify = o_tools, o_db, o_jd, o_notify
    return list(sent)


try:
    H.MARKER.unlink()
except OSError:
    pass

check('tools break -> notify', len(_drive(['tool down'], [], [])) == 1)
check('same state -> silent', len(_drive(['tool down'], [], [])) == 0)
# THE REGRESSION: a new subsystem failing while an old one persists.
check('tools still broken + JD newly stalled -> MUST notify',
      len(_drive(['tool down'], [], ['jd stalled'])) == 1)
check('same combined state -> silent',
      len(_drive(['tool down'], [], ['jd stalled'])) == 0)
check('tools recover, JD persists -> no spurious repeat',
      len(_drive([], [], ['jd stalled'])) == 0)
check('all recover -> state clears', len(_drive([], [], [])) == 0)
check('after recovery, a new failure alerts again',
      len(_drive([], [], ['jd stalled'])) == 1)

# An undelivered alert must NOT be recorded, or a Gotify blip silences it forever.
try:
    H.MARKER.unlink()
except OSError:
    pass
_drive(['tool down'], [], [], deliver=False)
check('failed delivery is retried, not suppressed',
      len(_drive(['tool down'], [], [])) == 1)

try:
    H.MARKER.unlink()
except OSError:
    pass
print()
print('FAILURES: %d' % fails)
sys.exit(1 if fails else 0)
