"""Bounded failure classification for file placement — safety-gate step 1.

Today every apply failure collapses into one free-text ``error_message``
column. That is enough to show a human a string and nothing else: you cannot
count it, alert on it, or — the part that matters here — answer the only
question a file-safety gate actually asks.

**What is the state of the files on disk after this failure?**

That, not the cause, is the primary axis. "Permission denied" tells you nothing
about whether a file was lost; "the destination now holds a half-written copy"
tells you everything, regardless of which errno produced it. So every failure
gets classified twice: a bounded ``Cause`` for counting and alerting, and a
``DiskOutcome`` for deciding what is safe to do next.

FAIL-CLOSED, and this is the whole point of the module: an unrecognised failure
classifies as ``UNKNOWN``, which is treated as the WORST case — not retry-safe
and operator-required. A gate that assumes an unclassified error was harmless
is how a silent loss gets retried into a louder one. Every widening of the
bounded set must be a deliberate edit here, with a test.

Nothing in this module performs I/O or touches the filesystem; it reasons about
an exception that already happened. That keeps the rules testable without
staging a real data-loss scenario, which is exactly the property the sweep's
completion.py had when its tests caught a bug a code read had missed.
"""
from __future__ import annotations

import errno
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    """Where in the apply the failure happened. Changes what is at risk."""
    PREFLIGHT = "preflight"                    # before anything was touched
    PLACEMENT = "placement"                    # inside place_file()
    POST_PLACEMENT_RECORD = "post_placement_record"  # file placed, DB write failed


class Cause(str, Enum):
    """Bounded, countable failure causes. UNCLASSIFIED is a real answer."""
    SOURCE_MISSING = "source_missing"
    DESTINATION_EXISTS = "destination_exists"
    CROSS_DEVICE = "cross_device"
    UNSUPPORTED_FILESYSTEM = "unsupported_filesystem"
    PERMISSION_DENIED = "permission_denied"
    NO_SPACE = "no_space"
    READ_ONLY_TARGET = "read_only_target"
    NAME_TOO_LONG = "name_too_long"
    IO_ERROR = "io_error"
    INTERRUPTED = "interrupted"
    DB_WRITE_FAILED = "db_write_failed"
    UNCLASSIFIED = "unclassified"


class DiskOutcome(str, Enum):
    """What the filesystem looks like now. The safety-relevant axis."""
    #: Source intact, destination untouched. The only genuinely clean failure.
    NO_OP = "no_op"
    #: A partial or unverified destination may exist and needs cleaning up.
    DEST_PARTIAL = "dest_partial"
    #: The file WAS placed but the record says otherwise — disk and DB disagree.
    MOVED_UNRECORDED = "moved_unrecorded"
    #: An overwrite trashed the previous occupant and the incoming file then
    #: failed to land (the SH-H09 window). Something must be restored.
    PRIOR_OCCUPANT_TRASHED = "prior_occupant_trashed"
    #: We cannot tell. Treated as the worst case, never as harmless.
    UNKNOWN = "unknown"


#: Only a proven no-op may be retried without human involvement. Everything
#: else either needs cleanup first or has left disk and records disagreeing.
RETRY_SAFE_OUTCOMES = frozenset({DiskOutcome.NO_OP})

_ERRNO_CAUSE = {
    errno.ENOENT: Cause.SOURCE_MISSING,
    errno.EEXIST: Cause.DESTINATION_EXISTS,
    errno.EXDEV: Cause.CROSS_DEVICE,
    errno.EACCES: Cause.PERMISSION_DENIED,
    errno.EPERM: Cause.PERMISSION_DENIED,
    errno.ENOSPC: Cause.NO_SPACE,
    errno.EDQUOT: Cause.NO_SPACE,
    errno.EROFS: Cause.READ_ONLY_TARGET,
    errno.ENAMETOOLONG: Cause.NAME_TOO_LONG,
    errno.EIO: Cause.IO_ERROR,
    errno.EINTR: Cause.INTERRUPTED,
    errno.ENOSYS: Cause.UNSUPPORTED_FILESYSTEM,
    errno.ENOTSUP: Cause.UNSUPPORTED_FILESYSTEM,
    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP): Cause.UNSUPPORTED_FILESYSTEM,
}

#: Causes that prove nothing was written to the destination. place_file()
#: refuses to overwrite and raises BEFORE publishing, and a missing source or a
#: rejected filesystem is detected before any bytes move.
_NO_OP_CAUSES = frozenset({
    Cause.SOURCE_MISSING,
    Cause.DESTINATION_EXISTS,
    Cause.UNSUPPORTED_FILESYSTEM,
})


@dataclass(frozen=True)
class FailureVerdict:
    cause: Cause
    disk_outcome: DiskOutcome
    phase: Phase
    detail: str
    errno_value: Optional[int] = None

    @property
    def retry_safe(self) -> bool:
        """True only when the filesystem is provably unchanged."""
        return self.disk_outcome in RETRY_SAFE_OUTCOMES

    @property
    def requires_operator(self) -> bool:
        """True when a human must look before anything else touches these paths."""
        return self.disk_outcome in (
            DiskOutcome.MOVED_UNRECORDED,
            DiskOutcome.PRIOR_OCCUPANT_TRASHED,
            DiskOutcome.UNKNOWN,
        )

    @property
    def is_classified(self) -> bool:
        return self.cause is not Cause.UNCLASSIFIED


def classify_failure(
    exc: BaseException,
    *,
    phase: Phase = Phase.PLACEMENT,
    prior_occupant_trashed: bool = False,
    bytes_written: Optional[int] = None,
) -> FailureVerdict:
    """Classify one apply failure.

    `prior_occupant_trashed` says an overwrite already trashed whatever held the
    destination. That fact OUTRANKS the cause: however the placement failed,
    the destination the library expects is now empty, so the outcome is
    PRIOR_OCCUPANT_TRASHED and a human decides.

    `bytes_written` distinguishes a copy that never started from one that died
    partway. None means unknown, which is not treated as zero.
    """
    cause, errno_value = _cause_of(exc)

    if phase is Phase.POST_PLACEMENT_RECORD:
        # The file is where it should be and the database disagrees. Retrying
        # would place it a second time; doing nothing leaves a job stuck. Both
        # are wrong without a human, and neither is safe to guess.
        return FailureVerdict(
            cause=Cause.DB_WRITE_FAILED if cause is Cause.UNCLASSIFIED else cause,
            disk_outcome=DiskOutcome.MOVED_UNRECORDED, phase=phase,
            detail=("the file was placed but the record was not written — "
                    "disk and database disagree"),
            errno_value=errno_value,
        )

    if prior_occupant_trashed:
        return FailureVerdict(
            cause=cause, disk_outcome=DiskOutcome.PRIOR_OCCUPANT_TRASHED,
            phase=phase,
            detail=("an overwrite trashed the previous occupant and the incoming "
                    f"file then failed to land ({_describe(exc)})"),
            errno_value=errno_value,
        )

    if cause is Cause.UNCLASSIFIED:
        # THE FAIL-CLOSED RULE. We do not know what this error did, so we do not
        # get to assume it did nothing.
        return FailureVerdict(
            cause=cause, disk_outcome=DiskOutcome.UNKNOWN, phase=phase,
            detail=(f"unrecognised failure ({_describe(exc)}) — treated as the "
                    "worst case until classified"),
            errno_value=errno_value,
        )

    if phase is Phase.PREFLIGHT or cause in _NO_OP_CAUSES:
        return FailureVerdict(
            cause=cause, disk_outcome=DiskOutcome.NO_OP, phase=phase,
            detail=f"nothing was written ({_describe(exc)})",
            errno_value=errno_value,
        )

    if bytes_written == 0:
        return FailureVerdict(
            cause=cause, disk_outcome=DiskOutcome.NO_OP, phase=phase,
            detail=f"the copy never began ({_describe(exc)})",
            errno_value=errno_value,
        )

    # A recognised failure during placement with bytes written, or an unknown
    # amount written. Either way a partial destination may exist.
    return FailureVerdict(
        cause=cause, disk_outcome=DiskOutcome.DEST_PARTIAL, phase=phase,
        detail=(f"placement failed after writing "
                f"{'an unknown number of' if bytes_written is None else bytes_written} "
                f"bytes ({_describe(exc)}) — the destination may hold a partial file"),
        errno_value=errno_value,
    )


def _cause_of(exc: BaseException):
    """Map an exception to a bounded cause, preferring errno over type."""
    # UnsupportedFilesystemSafetyError subclasses OSError and carries ENOTSUP,
    # so the errno table below already covers it without importing fileops.
    raw = getattr(exc, "errno", None)
    if isinstance(raw, int) and raw in _ERRNO_CAUSE:
        return _ERRNO_CAUSE[raw], raw

    # Type fallbacks for exceptions raised without an errno.
    if isinstance(exc, FileExistsError):
        return Cause.DESTINATION_EXISTS, raw
    if isinstance(exc, FileNotFoundError):
        return Cause.SOURCE_MISSING, raw
    if isinstance(exc, PermissionError):
        return Cause.PERMISSION_DENIED, raw
    if isinstance(exc, InterruptedError):
        return Cause.INTERRUPTED, raw
    return Cause.UNCLASSIFIED, raw if isinstance(raw, int) else None


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
