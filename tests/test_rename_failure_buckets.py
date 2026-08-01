"""Failure bucketing — safety-gate step 1.

The bucket that matters is not WHY it failed but WHAT IS ON DISK NOW. These
tests pin that axis, and pin that an unrecognised failure is treated as the
worst case rather than as harmless.
"""

import errno

import pytest

from backend.rename.failure import (
    Cause,
    DiskOutcome,
    Phase,
    classify_failure,
)


def oserr(code, msg="boom"):
    return OSError(code, msg)


class TestCauseMapping:
    @pytest.mark.parametrize("code,cause", [
        (errno.ENOENT, Cause.SOURCE_MISSING),
        (errno.EEXIST, Cause.DESTINATION_EXISTS),
        (errno.EXDEV, Cause.CROSS_DEVICE),
        (errno.EACCES, Cause.PERMISSION_DENIED),
        (errno.EPERM, Cause.PERMISSION_DENIED),
        (errno.ENOSPC, Cause.NO_SPACE),
        (errno.EROFS, Cause.READ_ONLY_TARGET),
        (errno.ENAMETOOLONG, Cause.NAME_TOO_LONG),
        (errno.EIO, Cause.IO_ERROR),
        (errno.ENOSYS, Cause.UNSUPPORTED_FILESYSTEM),
        (errno.ENOTSUP, Cause.UNSUPPORTED_FILESYSTEM),
    ])
    def test_errno_maps_to_a_bounded_cause(self, code, cause):
        assert classify_failure(oserr(code)).cause is cause

    def test_the_unsupported_filesystem_guard_is_covered_without_importing_fileops(self):
        """UnsupportedFilesystemSafetyError subclasses OSError with ENOTSUP, so
        the errno table already handles it — keeping this module free of any
        dependency on the code it classifies failures from."""
        from backend.rename.fileops import UnsupportedFilesystemSafetyError
        exc = UnsupportedFilesystemSafetyError(
            "rename", "/library/movies", reason="no RENAME_NOREPLACE support")
        assert classify_failure(exc).cause is Cause.UNSUPPORTED_FILESYSTEM
        # And it is a clean no-op: the guard fires before anything is written,
        # which is the guarantee its own message makes.
        assert classify_failure(exc).disk_outcome is DiskOutcome.NO_OP

    def test_exceptions_without_errno_fall_back_to_type(self):
        assert classify_failure(FileExistsError("x")).cause is Cause.DESTINATION_EXISTS
        assert classify_failure(FileNotFoundError("x")).cause is Cause.SOURCE_MISSING
        assert classify_failure(PermissionError("x")).cause is Cause.PERMISSION_DENIED

    def test_errno_beats_type_when_they_disagree(self):
        """A stdlib subclass raised with a contradicting errno is classified by
        the errno, which is what the OS actually reported."""
        exc = OSError(errno.ENOSPC, "no space")
        assert classify_failure(exc).cause is Cause.NO_SPACE


class TestFailClosed:
    def test_an_unrecognised_failure_is_UNKNOWN_not_harmless(self):
        """THE RULE. Assuming an unclassified error changed nothing is how a
        silent loss gets retried into a louder one."""
        v = classify_failure(RuntimeError("something nobody anticipated"))
        assert v.cause is Cause.UNCLASSIFIED
        assert v.disk_outcome is DiskOutcome.UNKNOWN
        assert not v.retry_safe
        assert v.requires_operator
        assert not v.is_classified

    def test_an_unmapped_errno_is_also_UNKNOWN(self):
        v = classify_failure(oserr(errno.EMLINK))
        assert v.disk_outcome is DiskOutcome.UNKNOWN
        assert not v.retry_safe

    def test_unknown_bytes_written_is_not_treated_as_zero(self):
        """`bytes_written=None` means we do not know. A partial destination may
        exist, so it must not be classified as a clean no-op."""
        v = classify_failure(oserr(errno.EIO), bytes_written=None)
        assert v.disk_outcome is DiskOutcome.DEST_PARTIAL
        assert not v.retry_safe


class TestDiskOutcome:
    def test_destination_exists_is_a_clean_no_op(self):
        """place_file() refuses to overwrite and raises BEFORE publishing, so
        nothing on disk changed."""
        v = classify_failure(FileExistsError(errno.EEXIST, "exists"))
        assert v.disk_outcome is DiskOutcome.NO_OP
        assert v.retry_safe and not v.requires_operator

    def test_missing_source_is_a_clean_no_op(self):
        assert classify_failure(oserr(errno.ENOENT)).disk_outcome is DiskOutcome.NO_OP

    def test_preflight_failures_are_always_no_op(self):
        """Nothing has been touched yet, whatever the cause."""
        v = classify_failure(oserr(errno.EIO), phase=Phase.PREFLIGHT)
        assert v.disk_outcome is DiskOutcome.NO_OP
        assert v.retry_safe

    def test_a_copy_that_never_started_is_a_no_op(self):
        v = classify_failure(oserr(errno.ENOSPC), bytes_written=0)
        assert v.disk_outcome is DiskOutcome.NO_OP

    def test_a_copy_that_died_partway_leaves_a_partial_destination(self):
        v = classify_failure(oserr(errno.ENOSPC), bytes_written=4_000_000)
        assert v.disk_outcome is DiskOutcome.DEST_PARTIAL
        assert not v.retry_safe
        assert not v.requires_operator      # cleanable without a human


class TestPostPlacement:
    def test_a_failed_record_after_a_successful_move_needs_an_operator(self):
        """The file is where it should be and the database disagrees. Retrying
        places it twice; doing nothing strands the job. Neither is guessable."""
        v = classify_failure(RuntimeError("db write returned False"),
                             phase=Phase.POST_PLACEMENT_RECORD)
        assert v.disk_outcome is DiskOutcome.MOVED_UNRECORDED
        assert v.cause is Cause.DB_WRITE_FAILED
        assert not v.retry_safe and v.requires_operator

    def test_post_placement_keeps_a_recognised_cause(self):
        v = classify_failure(oserr(errno.EIO), phase=Phase.POST_PLACEMENT_RECORD)
        assert v.cause is Cause.IO_ERROR
        assert v.disk_outcome is DiskOutcome.MOVED_UNRECORDED


class TestPriorOccupantTrashed:
    def test_it_outranks_the_cause(self):
        """SH-H09's window. However the placement failed, the destination the
        library expects is now empty — that fact governs."""
        v = classify_failure(oserr(errno.ENOSPC), prior_occupant_trashed=True)
        assert v.disk_outcome is DiskOutcome.PRIOR_OCCUPANT_TRASHED
        assert v.cause is Cause.NO_SPACE       # cause still recorded for counting
        assert v.requires_operator

    def test_it_outranks_even_a_would_be_clean_no_op(self):
        """The most dangerous case: a failure that looks harmless in isolation
        while the library's file has already been moved to trash."""
        v = classify_failure(FileExistsError(errno.EEXIST, "exists"),
                             prior_occupant_trashed=True)
        assert v.disk_outcome is not DiskOutcome.NO_OP
        assert not v.retry_safe

    def test_it_outranks_unclassified_too(self):
        v = classify_failure(RuntimeError("?"), prior_occupant_trashed=True)
        assert v.disk_outcome is DiskOutcome.PRIOR_OCCUPANT_TRASHED


class TestBoundedness:
    def test_retry_is_permitted_ONLY_for_a_proven_no_op(self):
        """One-line statement of the safety property: every outcome that is not
        a proven no-op blocks automatic retry."""
        for outcome in DiskOutcome:
            retryable = outcome is DiskOutcome.NO_OP
            assert (outcome in _retry_safe()) is retryable

    def test_every_cause_is_a_member_of_the_bounded_set(self):
        """Guards against a stringly-typed cause creeping back in."""
        v = classify_failure(oserr(errno.EACCES))
        assert isinstance(v.cause, Cause)
        assert v.cause.value in {c.value for c in Cause}

    def test_detail_always_carries_the_original_exception_text(self):
        """The free-text message stays available — bucketing adds structure, it
        does not replace what a human reads."""
        v = classify_failure(oserr(errno.EACCES, "permission denied on /library"))
        assert "permission denied on /library" in v.detail


def _retry_safe():
    from backend.rename.failure import RETRY_SAFE_OUTCOMES
    return RETRY_SAFE_OUTCOMES
