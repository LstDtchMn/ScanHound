"""Disk-state classification for file placement — safety-gate step 1 (rev 2).

Every apply failure used to collapse into one free-text ``error_message``. You
cannot count that, alert on it, or answer the only question a file-safety gate
asks: **what is the state of the files on disk now?**

REV 2 CORRECTS A DESIGN ERROR IN REV 1. That version named disk state as its
primary axis but *inferred* it from exception type, errno and bytes written.
That cannot be sound. A ``FileNotFoundError`` does not prove "source intact,
destination untouched" — it may have come from a later cleanup step, a sidecar,
a parent directory, or a source that vanished during a race. ``bytes_written ==
0`` does not prove nothing changed: a rename, hardlink, trash move or metadata
write may already have happened.

So classification now CONSUMES OBSERVED FACTS. The caller looks at the
filesystem and reports what it saw; this module decides what that means. Cause
is still derived from the exception, because a cause is a useful thing to count
— but it no longer determines safety.

Two axes, deliberately independent:

    Cause         why it failed         — for counting and alerting
    DiskOutcome   what is on disk now   — for deciding what is safe next

FAIL-CLOSED: every observed fact defaults to ``None`` meaning "not established",
and an incomplete observation yields ``UNKNOWN``, the worst case. "We did not
look" must never read as "nothing happened".
"""
from __future__ import annotations

import errno
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Phase(str, Enum):
    """Where in the apply the failure happened."""
    PREFLIGHT = "preflight"
    PLACEMENT = "placement"
    POST_PLACEMENT_RECORD = "post_placement_record"


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
    """What the filesystem looks like now, from observation.

    Source presence and destination completeness are represented independently.
    Conflating them is what hid two very different states in rev 1: a DUPLICATE
    (both exist, verified) and a LOSS (neither is usable).
    """
    #: Source present, destination absent. The only genuinely clean failure.
    UNCHANGED = "unchanged"
    #: A partial or unverified destination beside an intact source. Cleanable
    #: mechanically: remove the partial, retry.
    DEST_PARTIAL_SOURCE_PRESENT = "dest_partial_source_present"
    #: Both exist, destination verified complete. A copy finished but the source
    #: was not consumed — a cross-device move that never cleaned up, or a failed
    #: source deletion. Not partial and not lost: DUPLICATED and unfinalised.
    DEST_COMPLETE_SOURCE_PRESENT = "dest_complete_source_present"
    #: The placement genuinely completed on disk. Dangerous only if the record
    #: disagrees.
    DEST_COMPLETE_SOURCE_ABSENT = "dest_complete_source_absent"
    #: THE CATASTROPHIC STATE. The original is gone and no verified complete
    #: replacement exists. Named separately so it can never disappear into
    #: UNKNOWN — it demands a distinct emergency recovery path.
    SOURCE_ABSENT_DEST_UNUSABLE = "source_absent_dest_unusable"
    #: An overwrite displaced whatever held the destination and the incoming
    #: file did not land. Something must be restored.
    PRIOR_OCCUPANT_DISPLACED = "prior_occupant_displaced"
    #: We cannot tell. Treated as the worst case, never as harmless.
    UNKNOWN = "unknown"


#: Only a proven no-op may be retried without human involvement.
RETRY_SAFE_OUTCOMES = frozenset({DiskOutcome.UNCHANGED})

#: A partial destination beside an intact source can be cleaned up mechanically.
AUTO_CLEANABLE_OUTCOMES = frozenset({DiskOutcome.DEST_PARTIAL_SOURCE_PRESENT})

#: States where a human must look before anything else touches these paths.
OPERATOR_REQUIRED_OUTCOMES = frozenset({
    DiskOutcome.SOURCE_ABSENT_DEST_UNUSABLE,
    DiskOutcome.PRIOR_OCCUPANT_DISPLACED,
    DiskOutcome.DEST_COMPLETE_SOURCE_PRESENT,
    DiskOutcome.UNKNOWN,
})


@dataclass(frozen=True)
class DiskObservation:
    """Facts the caller established by LOOKING at the filesystem.

    Every field defaults to None meaning "not established". None is not False:
    an unchecked destination is not an absent one, and that difference decides
    whether a state is clean or catastrophic.
    """
    source_present: Optional[bool] = None
    destination_present: Optional[bool] = None
    #: Verified complete — size and/or hash checked, not merely existing.
    destination_complete: Optional[bool] = None
    #: The destination path still holds the file that was there BEFORE this
    #: operation.
    destination_is_prior_occupant: Optional[bool] = None
    prior_occupant_trashed: bool = False
    prior_occupant_restored: bool = False
    temp_path_present: Optional[bool] = None
    method: Optional[str] = None
    last_confirmed_phase: Optional[Phase] = None

    @property
    def is_complete(self) -> bool:
        """True when the facts needed for a confident verdict were established.

        A destination that is absent needs no completeness check; one that is
        present does.
        """
        if self.source_present is None or self.destination_present is None:
            return False
        if self.destination_present and self.destination_complete is None:
            return False
        return True


@dataclass(frozen=True)
class FailureVerdict:
    cause: Cause
    disk_outcome: DiskOutcome
    phase: Phase
    detail: str
    errno_value: Optional[int] = None
    observation: Optional[DiskObservation] = None

    @property
    def retry_safe(self) -> bool:
        return self.disk_outcome in RETRY_SAFE_OUTCOMES

    @property
    def auto_cleanable(self) -> bool:
        return self.disk_outcome in AUTO_CLEANABLE_OUTCOMES

    @property
    def requires_operator(self) -> bool:
        return self.disk_outcome in OPERATOR_REQUIRED_OUTCOMES

    @property
    def is_catastrophic(self) -> bool:
        """The original is gone with no verified replacement."""
        return self.disk_outcome is DiskOutcome.SOURCE_ABSENT_DEST_UNUSABLE

    @property
    def is_classified(self) -> bool:
        return self.cause is not Cause.UNCLASSIFIED


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


def classify_cause(exc: Optional[BaseException]):
    """Map an exception to a bounded cause. METRICS ONLY — never safety."""
    if exc is None:
        return Cause.UNCLASSIFIED, None
    raw = getattr(exc, "errno", None)
    if isinstance(raw, int) and raw in _ERRNO_CAUSE:
        return _ERRNO_CAUSE[raw], raw
    if isinstance(exc, FileExistsError):
        return Cause.DESTINATION_EXISTS, raw
    if isinstance(exc, FileNotFoundError):
        return Cause.SOURCE_MISSING, raw
    if isinstance(exc, PermissionError):
        return Cause.PERMISSION_DENIED, raw
    if isinstance(exc, InterruptedError):
        return Cause.INTERRUPTED, raw
    return Cause.UNCLASSIFIED, raw if isinstance(raw, int) else None


def classify_failure(
    exc: Optional[BaseException],
    observation: Optional[DiskObservation] = None,
    *,
    phase: Phase = Phase.PLACEMENT,
) -> FailureVerdict:
    """Classify one apply failure from OBSERVED disk state.

    `observation` is what the caller saw when it looked. Without it, or with it
    incomplete, the verdict is UNKNOWN — because rev 1's mistake was deciding
    that certain exceptions imply an untouched filesystem, and they do not.
    """
    cause, errno_value = classify_cause(exc)
    obs = observation or DiskObservation()

    if phase is Phase.POST_PLACEMENT_RECORD:
        # The file is where it should be and the database disagrees. Retrying
        # would place it twice; doing nothing strands the job. Neither is
        # guessable, and it is only a known state if we actually looked.
        outcome = (DiskOutcome.DEST_COMPLETE_SOURCE_ABSENT
                   if obs.destination_complete and obs.source_present is False
                   else DiskOutcome.UNKNOWN)
        return FailureVerdict(
            cause=Cause.DB_WRITE_FAILED if cause is Cause.UNCLASSIFIED else cause,
            disk_outcome=outcome, phase=phase,
            detail=("the file was placed but the record was not written — "
                    "disk and database disagree"),
            errno_value=errno_value, observation=obs)

    # An overwrite that displaced the previous occupant outranks everything:
    # however the placement failed, the file the library expects is no longer
    # where it expects it.
    if obs.prior_occupant_trashed and not obs.prior_occupant_restored:
        return _verdict(cause, DiskOutcome.PRIOR_OCCUPANT_DISPLACED, phase, obs,
                        errno_value,
                        "an overwrite displaced the previous occupant and the "
                        f"incoming file did not land ({_describe(exc)})")

    if not obs.is_complete:
        return _verdict(cause, DiskOutcome.UNKNOWN, phase, obs, errno_value,
                        "disk state was not established, so no safe conclusion "
                        f"is available ({_describe(exc)})")

    src, dst, complete = obs.source_present, obs.destination_present, bool(obs.destination_complete)

    if src and not dst:
        return _verdict(cause, DiskOutcome.UNCHANGED, phase, obs, errno_value,
                        f"source intact, destination absent ({_describe(exc)})")
    if src and dst and complete:
        return _verdict(cause, DiskOutcome.DEST_COMPLETE_SOURCE_PRESENT, phase,
                        obs, errno_value,
                        "a verified complete destination exists alongside the "
                        "source — duplicated, not lost, and not finalised "
                        f"({_describe(exc)})")
    if src and dst and not complete:
        return _verdict(cause, DiskOutcome.DEST_PARTIAL_SOURCE_PRESENT, phase,
                        obs, errno_value,
                        "an unverified destination exists beside an intact "
                        f"source ({_describe(exc)})")
    if not src and dst and complete:
        return _verdict(cause, DiskOutcome.DEST_COMPLETE_SOURCE_ABSENT, phase,
                        obs, errno_value,
                        f"the placement completed on disk ({_describe(exc)})")
    # Source gone, destination absent or unverified.
    return _verdict(cause, DiskOutcome.SOURCE_ABSENT_DEST_UNUSABLE, phase, obs,
                    errno_value,
                    "THE SOURCE IS GONE AND NO VERIFIED COMPLETE REPLACEMENT "
                    f"EXISTS ({_describe(exc)})")


def _verdict(cause, outcome, phase, obs, errno_value, detail):
    return FailureVerdict(cause=cause, disk_outcome=outcome, phase=phase,
                          detail=detail, errno_value=errno_value, observation=obs)


def _describe(exc: Optional[BaseException]) -> str:
    if exc is None:
        return "no exception recorded"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
