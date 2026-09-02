"""Is this path really on the network share it is supposed to be on?

Why this exists (2026-09-01). ``/library/tv`` -- the TV download, extraction
and rename destination -- is a bind of a NAS share mounted into the Docker VM
by a host-side task. Docker resolves that bind when the container is
CREATED. If the share was not mounted at that moment, or goes away later and
the container is recreated, ``/library/tv`` is an ordinary empty directory
inside the VM that looks exactly like the share. Every TV rename then
"succeeds" into a folder Plex will never see and that vanishes with the VM.
That was the 2026-07-26 outage.

Until now the ONLY protection was the host task stopping the whole container
whenever it could not prove the share -- so a two-day NAS outage was a
two-day ScanHound outage, although crawling, HDEncode, movie renames on the
local drives and the web UI need no NAS at all. This module moves the
protection to where the risk is: the WRITE. A share-backed destination is
verified at the moment of writing, by the same identity rule the host task
uses, and a write to an unverified one is refused with a reason. The app
keeps running; only the TV pipeline is unavailable while the share is.

The identity rule, mirrored from scripts/mount-nas-shares.ps1 so there is
ONE definition of "this is the share":

    the root is itself a mountpoint (an entry in /proc/self/mountinfo);
    its filesystem type is 9p (what a drvfs-mounted UNC path looks like
        from inside the VM and the container);
    its mount origin names the expected UNC share.

Anything else -- not a mountpoint, another filesystem, another share, or a
mount table that cannot be read -- is NOT verified. Unknown is not clean.

Paths that are not under a configured share-backed root are simply not this
module's business and pass through: the guard is about the TV share, not
about every file the app touches.
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)

#: Reported on /health and read by the host recovery task, which leaves a
#: running container alone under a share outage only when the container can
#: say it carries this guard. Bump when the guard's coverage changes in a way
#: the task should know about.
GUARD_VERSION = 1

MOUNTINFO_PATH = "/proc/self/mountinfo"

#: ``<container path> => <SERVER>\<share>`` per line (or ';'-separated).
#: The default is the one share that is a WRITE destination. The eight
#: read-only Plex sources are not listed: a blind read-only source yields
#: "no file here", never a misplaced write.
DEFAULT_SHARE_BACKED_ROOTS = "/library/tv => TURTLELANDSRV2\\k"


class ShareNotVerifiedError(RuntimeError):
    """A write was asked for on a share-backed root that is not verified."""


@dataclass(frozen=True)
class ShareVerdict:
    root: Optional[str]
    state: str          # verified | blind | unknown | not-share-backed | bypassed
    reason: str
    mountpoint: Optional[str] = None
    fstype: Optional[str] = None
    origin: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.state in ("verified", "not-share-backed", "bypassed")

    def as_dict(self) -> dict:
        return {
            "root": self.root, "state": self.state, "reason": self.reason,
            "mountpoint": self.mountpoint, "fstype": self.fstype, "origin": self.origin,
        }


_LOCK = threading.RLock()
# normalised root -> (display root, expected "SERVER\share" or None)
_ROOTS: Dict[str, tuple] = {}
_TEST_BYPASS_DEPTH = 0


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def parse_share_backed_roots(spec: Optional[str]) -> Dict[str, tuple]:
    """``"/library/tv => TURTLELANDSRV2\\k"`` lines into {norm_root: (display, expected)}.

    An entry without ``=>`` is a root whose share origin is not checked (any
    9p mount is accepted there). Blank lines and ``#`` comments are ignored.
    A malformed spec raises ValueError -- the caller decides whether that is
    fatal; the app falls back to the default rather than running unguarded.
    """
    out: Dict[str, tuple] = {}
    text = spec if spec is not None else ""
    for raw in text.replace(";", "\n").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            root, expected = (part.strip() for part in line.split("=>", 1))
            if not expected:
                raise ValueError("share_backed_roots: empty share after '=>' in %r" % line)
        else:
            root, expected = line, None
        if not root:
            raise ValueError("share_backed_roots: empty path in %r" % line)
        out[_norm(root)] = (root, expected)
    return out


def configure(spec: Optional[str]) -> None:
    """Install the roots to guard. ``None``/empty means the default.

    A malformed spec is logged and the DEFAULT is installed, never nothing:
    a configuration typo must not remove the guard from the TV share.
    """
    global _ROOTS
    try:
        roots = parse_share_backed_roots(spec) if (spec and spec.strip()) else None
    except ValueError as exc:
        logger.error("share_backed_roots is malformed (%s); guarding the default root instead", exc)
        roots = None
    if roots is None:
        roots = parse_share_backed_roots(DEFAULT_SHARE_BACKED_ROOTS)
    with _LOCK:
        _ROOTS = roots


def configured_roots() -> List[str]:
    with _LOCK:
        return [display for display, _ in _ROOTS.values()]


def _root_for(path: str) -> Optional[str]:
    """The longest configured root that contains ``path`` (normalised), or None."""
    target = _norm(path)
    with _LOCK:
        roots = list(_ROOTS.keys())
    best = None
    for root in roots:
        if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
            if best is None or len(root) > len(best):
                best = root
    return best


def _unescape(field: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as \040 \011 \012 \134
    out = []
    i = 0
    while i < len(field):
        ch = field[i]
        if ch == "\\" and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            try:
                out.append(chr(int(field[i + 1:i + 4], 8)))
                i += 4
                continue
            except ValueError:
                pass
        out.append(ch)
        i += 1
    return "".join(out)


def parse_mountinfo(text: str) -> List[dict]:
    """/proc/self/mountinfo lines -> [{mountpoint, fstype, source, superopts}].

    Unparseable lines are skipped; the caller treats an EMPTY result as
    unknown, so a table this cannot read at all never verifies anything.
    """
    entries: List[dict] = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        before, after = line.split(" - ", 1)
        bf = before.split()
        af = after.split()
        if len(bf) < 5 or len(af) < 2:
            continue
        entries.append({
            "mountpoint": _unescape(bf[4]),
            "fstype": af[0],
            "source": _unescape(af[1]),
            "superopts": _unescape(af[2]) if len(af) > 2 else "",
        })
    return entries


def _read_mountinfo() -> str:
    with open(MOUNTINFO_PATH, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def classify(path: str, *, mountinfo: Optional[str] = None) -> ShareVerdict:
    """Decide whether ``path`` sits on a verified share. Never raises."""
    root = _root_for(path)
    if root is None:
        return ShareVerdict(None, "not-share-backed", "not under a share-backed root")
    with _LOCK:
        display, expected = _ROOTS.get(root, (root, None))
    try:
        text = mountinfo if mountinfo is not None else _read_mountinfo()
    except Exception as exc:  # noqa: BLE001 -- unknown is the verdict, not a crash
        return ShareVerdict(display, "unknown", "mount table unreadable: %s" % exc)
    entries = parse_mountinfo(text)
    if not entries:
        return ShareVerdict(display, "unknown", "mount table is empty or unparseable")
    # Last matching entry wins, as in the host task (`tail -1`): a mount
    # stacked over an earlier one at the same path is the one that is live.
    match = None
    for entry in entries:
        if _norm(entry["mountpoint"]) == root:
            match = entry
    if match is None:
        return ShareVerdict(display, "blind", "%s is not a mountpoint; it is a plain directory inside the VM" % display)
    origin = "%s %s" % (match["source"], match["superopts"])
    if match["fstype"] != "9p":
        return ShareVerdict(display, "blind", "%s is mounted, but as %s, not the 9p share" % (display, match["fstype"]),
                            mountpoint=match["mountpoint"], fstype=match["fstype"], origin=match["source"])
    if expected is not None:
        needle = ("path=UNC\\" + expected).lower()
        if needle not in origin.lower().replace("/", "\\"):
            return ShareVerdict(display, "blind", "%s is a 9p mount of the wrong share (expected %s)" % (display, expected),
                                mountpoint=match["mountpoint"], fstype=match["fstype"], origin=match["source"])
    return ShareVerdict(display, "verified", "9p mount of the expected share",
                        mountpoint=match["mountpoint"], fstype=match["fstype"], origin=match["source"])


def require_share_backed(path: str, *, operation: str) -> ShareVerdict:
    """Refuse ``operation`` on ``path`` unless its share-backed root is verified.

    Called by every filesystem mutation BEFORE it creates anything -- a
    ``makedirs`` into a blind root is already the accident.
    """
    with _LOCK:
        bypassed = _TEST_BYPASS_DEPTH > 0
    if bypassed:
        return ShareVerdict(None, "bypassed", "share verification bypassed for tests")
    verdict = classify(path)
    if not verdict.ok:
        raise ShareNotVerifiedError(
            "%s refused: %s -- %s. Nothing was written. The TV share is unavailable; "
            "retry when it is back (see /health share_backed_roots)."
            % (operation, verdict.root, verdict.reason))
    return verdict


def status() -> dict:
    """For /health: every configured root's verdict right now. Never raises."""
    with _LOCK:
        roots = list(_ROOTS.values())
    out = {}
    for display, _expected in roots:
        try:
            out[display] = classify(display).as_dict()
        except Exception as exc:  # noqa: BLE001
            out[display] = {"root": display, "state": "unknown", "reason": "status failed: %s" % exc}
    return {"guard_version": GUARD_VERSION, "roots": out}


@contextlib.contextmanager
def _unverified_shares_for_tests() -> Iterator[None]:
    """Direct fileops unit tests that write under a share-backed root."""
    global _TEST_BYPASS_DEPTH
    with _LOCK:
        _TEST_BYPASS_DEPTH += 1
    try:
        yield
    finally:
        with _LOCK:
            _TEST_BYPASS_DEPTH -= 1


configure(None)
