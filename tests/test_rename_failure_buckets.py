"""Disk-state classification — safety-gate step 1 (rev 2).

The bucket that matters is not WHY it failed but WHAT IS ON DISK NOW, and rev 1
got that wrong: it named disk state as the primary axis while inferring it from
exception type. These tests pin the corrected contract — classification consumes
observed facts, and anything unobserved is UNKNOWN.
"""

import errno

import pytest

from backend.rename.failure import (
    Cause,
    DiskObservation,
    DiskOutcome,
    Phase,
    classify_cause,
    classify_failure,
)


def obs(**kw):
    return DiskObservation(**kw)


INTACT = dict(source_present=True, destination_present=False)


class TestCauseIsMetricsOnly:
    @pytest.mark.parametrize("code,cause", [
        (errno.ENOENT, Cause.SOURCE_MISSING),
        (errno.EEXIST, Cause.DESTINATION_EXISTS),
        (errno.EXDEV, Cause.CROSS_DEVICE),
        (errno.EACCES, Cause.PERMISSION_DENIED),
        (errno.ENOSPC, Cause.NO_SPACE),
        (errno.EROFS, Cause.READ_ONLY_TARGET),
        (errno.EIO, Cause.IO_ERROR),
        (errno.ENOTSUP, Cause.UNSUPPORTED_FILESYSTEM),
    ])
    def test_errno_maps_to_a_bounded_cause(self, code, cause):
        assert classify_cause(OSError(code, "x"))[0] is cause

    def test_the_unsupported_filesystem_guard_is_covered(self):
        from backend.rename.fileops import UnsupportedFilesystemSafetyError
        exc = UnsupportedFilesystemSafetyError(
            "rename", "/library/movies", reason="no RENAME_NOREPLACE")
        assert classify_cause(exc)[0] is Cause.UNSUPPORTED_FILESYSTEM

    def test_cause_NO_LONGER_decides_disk_state(self):
        """REV 1's ERROR. A FileNotFoundError was taken to prove "source intact,
        destination untouched". It proves nothing of the kind — it may have come
        from a later cleanup step, a sidecar, or a parent directory."""
        v = classify_failure(FileNotFoundError(errno.ENOENT, "gone"))
        assert v.cause is Cause.SOURCE_MISSING        # still counted
        assert v.disk_outcome is DiskOutcome.UNKNOWN  # but proves nothing


class TestUnobservedIsUnknown:
    def test_no_observation_at_all_is_UNKNOWN(self):
        assert classify_failure(OSError(errno.EIO, "io")).disk_outcome is \
            DiskOutcome.UNKNOWN

    def test_a_partial_observation_is_UNKNOWN(self):
        """Source checked, destination not. "We did not look" is not "absent"."""
        v = classify_failure(OSError(errno.EIO, "io"), obs(source_present=True))
        assert v.disk_outcome is DiskOutcome.UNKNOWN

    def test_a_present_destination_needs_a_completeness_verdict(self):
        v = classify_failure(None, obs(source_present=True,
                                       destination_present=True))
        assert v.disk_outcome is DiskOutcome.UNKNOWN

    def test_an_absent_destination_needs_no_completeness_check(self):
        assert classify_failure(None, obs(**INTACT)).disk_outcome is \
            DiskOutcome.UNCHANGED


class TestTheSevenStates:
    def test_unchanged(self):
        v = classify_failure(OSError(errno.EEXIST, "exists"), obs(**INTACT))
        assert v.disk_outcome is DiskOutcome.UNCHANGED
        assert v.retry_safe and not v.requires_operator

    def test_partial_destination_beside_an_intact_source(self):
        v = classify_failure(OSError(errno.ENOSPC, "full"),
                             obs(source_present=True, destination_present=True,
                                 destination_complete=False))
        assert v.disk_outcome is DiskOutcome.DEST_PARTIAL_SOURCE_PRESENT
        assert v.auto_cleanable and not v.retry_safe

    def test_DUPLICATE_is_its_own_state(self):
        """A copy finished but the source was not consumed. Rev 1 had no way to
        say this — it was neither partial nor moved-unrecorded."""
        v = classify_failure(OSError(errno.EACCES, "denied"),
                             obs(source_present=True, destination_present=True,
                                 destination_complete=True))
        assert v.disk_outcome is DiskOutcome.DEST_COMPLETE_SOURCE_PRESENT
        assert v.requires_operator
        assert "duplicated, not lost" in v.detail

    def test_completed_on_disk(self):
        v = classify_failure(None, obs(source_present=False,
                                       destination_present=True,
                                       destination_complete=True))
        assert v.disk_outcome is DiskOutcome.DEST_COMPLETE_SOURCE_ABSENT

    def test_CATASTROPHIC_source_gone_destination_absent(self):
        """The state that must never hide inside UNKNOWN."""
        v = classify_failure(OSError(errno.EIO, "io"),
                             obs(source_present=False, destination_present=False))
        assert v.disk_outcome is DiskOutcome.SOURCE_ABSENT_DEST_UNUSABLE
        assert v.is_catastrophic and v.requires_operator and not v.retry_safe

    def test_CATASTROPHIC_source_gone_destination_unverified(self):
        """A destination that exists but is not verified complete is not a
        replacement for the original."""
        v = classify_failure(OSError(errno.EIO, "io"),
                             obs(source_present=False, destination_present=True,
                                 destination_complete=False))
        assert v.disk_outcome is DiskOutcome.SOURCE_ABSENT_DEST_UNUSABLE
        assert v.is_catastrophic

    def test_prior_occupant_displaced(self):
        v = classify_failure(OSError(errno.ENOSPC, "full"),
                             obs(prior_occupant_trashed=True, **INTACT))
        assert v.disk_outcome is DiskOutcome.PRIOR_OCCUPANT_DISPLACED
        assert v.requires_operator

    def test_displacement_outranks_an_otherwise_clean_state(self):
        """The most dangerous case: everything looks fine while the library's
        own file has already been moved to trash."""
        v = classify_failure(FileExistsError(errno.EEXIST, "exists"),
                             obs(prior_occupant_trashed=True, **INTACT))
        assert v.disk_outcome is not DiskOutcome.UNCHANGED
        assert not v.retry_safe

    def test_a_restored_prior_occupant_does_not_trigger_it(self):
        v = classify_failure(None, obs(prior_occupant_trashed=True,
                                       prior_occupant_restored=True, **INTACT))
        assert v.disk_outcome is DiskOutcome.UNCHANGED


class TestPostPlacement:
    def test_a_verified_placement_with_a_failed_record(self):
        v = classify_failure(RuntimeError("db write returned False"),
                             obs(source_present=False, destination_present=True,
                                 destination_complete=True),
                             phase=Phase.POST_PLACEMENT_RECORD)
        assert v.disk_outcome is DiskOutcome.DEST_COMPLETE_SOURCE_ABSENT
        assert v.cause is Cause.DB_WRITE_FAILED

    def test_an_unobserved_post_placement_failure_is_UNKNOWN(self):
        v = classify_failure(RuntimeError("db"), phase=Phase.POST_PLACEMENT_RECORD)
        assert v.disk_outcome is DiskOutcome.UNKNOWN
        assert v.requires_operator


class TestSafetyProperties:
    def test_retry_is_permitted_ONLY_for_a_proven_no_op(self):
        from backend.rename.failure import RETRY_SAFE_OUTCOMES
        for outcome in DiskOutcome:
            assert (outcome in RETRY_SAFE_OUTCOMES) is (
                outcome is DiskOutcome.UNCHANGED)

    def test_every_loss_bearing_state_requires_an_operator(self):
        from backend.rename.failure import OPERATOR_REQUIRED_OUTCOMES
        for outcome in (DiskOutcome.SOURCE_ABSENT_DEST_UNUSABLE,
                        DiskOutcome.PRIOR_OCCUPANT_DISPLACED,
                        DiskOutcome.DEST_COMPLETE_SOURCE_PRESENT,
                        DiskOutcome.UNKNOWN):
            assert outcome in OPERATOR_REQUIRED_OUTCOMES

    def test_source_presence_and_destination_completeness_are_independent(self):
        """The axis rev 1 collapsed. Same source state, opposite verdicts."""
        dup = classify_failure(None, obs(source_present=True,
                                         destination_present=True,
                                         destination_complete=True))
        partial = classify_failure(None, obs(source_present=True,
                                             destination_present=True,
                                             destination_complete=False))
        assert dup.disk_outcome is not partial.disk_outcome
        assert dup.requires_operator and partial.auto_cleanable

    def test_detail_carries_the_original_exception_text(self):
        v = classify_failure(OSError(errno.EACCES, "denied on /library"),
                             obs(**INTACT))
        assert "denied on /library" in v.detail

    def test_the_observation_is_retained_on_the_verdict(self):
        """So an audit can see what was actually looked at."""
        o = obs(**INTACT)
        assert classify_failure(None, o).observation is o
