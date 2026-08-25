"""The swallowed-failure checker must catch the defects it was built for.

A checker written from a description of a bug will happily pass on a fixture
written from the same description. So these fixtures are the ACTUAL code as it
stood when each defect shipped, reconstructed from the commits, paired with the
fixed version which must not be reported.

If these ever stop failing on the "before" fixtures, the checker has become
theatre and should be deleted rather than trusted.
"""

import os
import subprocess
import sys
import textwrap

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINT = os.path.join(REPO, "scripts", "lint_swallowed_failures.py")


def run_lint(tmp_path, source, name="sample.py"):
    """Run the checker over one file; return (exit_code, rules, stdout)."""
    p = tmp_path / name
    p.write_text(textwrap.dedent(source), encoding="utf-8")
    r = subprocess.run([sys.executable, LINT, str(p)],
                       capture_output=True, text=True)
    rules = {ln.split("[", 1)[1].split("]", 1)[0]
             for ln in r.stdout.splitlines() if "[" in ln and "]" in ln}
    return r.returncode, rules, r.stdout


# =========================================================================
# DEFECT 1 -- round 26's inert guard, as shipped.
# =========================================================================

INERT_GUARD_BEFORE = '''
    import logging
    import os
    logger = logging.getLogger(__name__)


    class DatabaseManager:
        def _quarantine_corrupt_db(self, e):
            if os.path.exists(self.db_path):
                backup = self.db_path + ".corrupt"
                try:
                    os.rename(self.db_path, backup)
                    stranded = [self.db_path + "-wal"]
                    if stranded:
                        raise OSError("refusing to create a fresh one")
                    self.init_db()
                except OSError as os_err:
                    logger.critical("Failed to recover DB: %s", os_err)
'''

INERT_GUARD_AFTER = '''
    import logging
    import os
    logger = logging.getLogger(__name__)


    class QuarantineIncomplete(RuntimeError):
        pass


    class DatabaseManager:
        def _quarantine_corrupt_db(self, e):
            if os.path.exists(self.db_path):
                backup = self.db_path + ".corrupt"
                try:
                    os.rename(self.db_path, backup)
                    stranded = [self.db_path + "-wal"]
                    if stranded:
                        raise QuarantineIncomplete("refusing")
                    self.init_db()
                except QuarantineIncomplete:
                    raise
                except OSError as os_err:
                    logger.critical("Failed to recover DB: %s", os_err)
                    raise QuarantineIncomplete("incomplete") from os_err
'''


class TestTheInertGuardIsCaught:
    """Round 26. `raise OSError` landing in its own `except OSError`."""

    def test_the_defect_as_shipped_is_REPORTED(self, tmp_path):
        code, rules, out = run_lint(tmp_path, INERT_GUARD_BEFORE)
        assert "inert-guard" in rules, out
        assert code == 1, "a defect must gate CI"

    def test_the_fixed_version_is_clean(self, tmp_path):
        code, rules, out = run_lint(tmp_path, INERT_GUARD_AFTER)
        assert "inert-guard" not in rules, out

    def test_it_understands_the_exception_HIERARCHY(self, tmp_path):
        """`except OSError` catches PermissionError. Name matching would miss
        this, and the real defect relied on exactly that relationship."""
        code, rules, out = run_lint(tmp_path, '''
            import logging
            logger = logging.getLogger(__name__)


            class DatabaseManager:
                def _quarantine_corrupt_db(self, e):
                    try:
                        if True:
                            raise PermissionError("refuse")
                    except OSError as err:
                        logger.critical("oh well: %s", err)
        ''')
        assert "inert-guard" in rules, out


# =========================================================================
# DEFECT 2 -- round 27's swallowed close. Contains no `raise` at all.
# =========================================================================

SWALLOWED_CLOSE_BEFORE = '''
    import sqlite3


    class DatabaseManager:
        def _quarantine_corrupt_db(self, e):
            if self.conn:
                try:
                    self.conn.close()
                except sqlite3.Error:
                    pass
                self.conn = None
            self._rename_bundle()
'''

SWALLOWED_CLOSE_AFTER = '''
    import sqlite3


    class QuarantineIncomplete(RuntimeError):
        pass


    class DatabaseManager:
        def _quarantine_corrupt_db(self, e):
            if self.conn:
                try:
                    self.conn.close()
                except BaseException as close_err:
                    raise QuarantineIncomplete("cannot prove closed") from close_err
                self.conn = None
            self._rename_bundle()
'''


class TestTheSwallowedPreconditionIsCaught:
    """Round 27. No `raise` anywhere in it, which is why grep could not find
    it and why this rule keys on the HANDLER rather than on raises."""

    def test_the_defect_as_shipped_is_REPORTED(self, tmp_path):
        code, rules, out = run_lint(tmp_path, SWALLOWED_CLOSE_BEFORE)
        assert "swallowed-at-boundary" in rules, out
        assert code == 1

    def test_the_fixed_version_is_clean(self, tmp_path):
        code, rules, out = run_lint(tmp_path, SWALLOWED_CLOSE_AFTER)
        assert "swallowed-at-boundary" not in rules, out

    def test_the_SAME_code_outside_a_safety_boundary_is_not_reported(
            self, tmp_path):
        """Anti-vacuity, and the rule's deliberate limit: absorbing a failure is
        only a defect where absorbing is not allowed. A checker that flagged
        every broad handler in the codebase would be turned off in a week."""
        code, rules, out = run_lint(tmp_path, '''
            import sqlite3


            class DatabaseManager:
                def refresh_thumbnail_cache(self, e):
                    if self.conn:
                        try:
                            self.conn.close()
                        except sqlite3.Error:
                            pass
                        self.conn = None
        ''')
        assert "swallowed-at-boundary" not in rules, out


# =========================================================================
# The distinctions that keep it usable.
# =========================================================================

class TestItDistinguishesDeliberateControlFlowFromTheDefect:
    """Found by running it on the real codebase, which immediately produced a
    false-positive class I had not anticipated: raise-to-converge-on-your-own
    -handler. `backend/rename/service.py` even documents doing it."""

    def test_a_handler_that_returns_a_typed_failure_is_only_flagged_to_VERIFY(
            self, tmp_path):
        code, rules, out = run_lint(tmp_path, '''
            class Service:
                def apply(self):
                    try:
                        if not self.db.write():
                            raise RuntimeError("write did not persist")
                        return {"ok": True}
                    except Exception as exc:
                        self.rollback()
                        return {"ok": False, "error": str(exc)}
        ''')
        assert "guard-reaches-own-handler" in rules, out
        assert "inert-guard" not in rules, out
        assert code == 0, "a 'verify' line must not gate CI"

    def test_a_swallow_followed_by_an_unconditional_raise_is_not_flagged(
            self, tmp_path):
        """Also found on the real codebase, in code written the same day: the
        handler absorbs a failure to read an optional detail, and the function
        raises regardless. Judging the handler in isolation was the wrong unit
        of analysis."""
        code, rules, out = run_lint(tmp_path, '''
            import json


            class QuarantineIncomplete(RuntimeError):
                pass


            class DatabaseManager:
                def _refuse_if_quarantine_pending(self):
                    detail = ""
                    try:
                        with open(self._pending_path) as f:
                            detail = json.load(f).get("reason", "")
                    except (OSError, ValueError):
                        pass
                    raise QuarantineIncomplete("did not complete: %s" % detail)
        ''')
        assert rules == set(), out


class TestSuppressionsMustBeJustified:
    def test_a_reasoned_suppression_silences_it(self, tmp_path):
        code, rules, out = run_lint(tmp_path, '''
            import logging
            logger = logging.getLogger(__name__)


            class DatabaseManager:
                def _notify_corruption(self, error):
                    try:
                        self.bridge.notify_error(str(error))
                    except Exception:  # fail-soft-ok: bonus channel, the log is primary
                        logger.debug("unavailable")
        ''')
        assert rules == set(), out
        assert code == 0

    def test_a_BARE_suppression_is_itself_reported(self, tmp_path):
        """A suppression nobody had to justify is how the next one of these
        gets waved through."""
        code, rules, out = run_lint(tmp_path, '''
            import logging
            logger = logging.getLogger(__name__)


            class DatabaseManager:
                def _quarantine_corrupt_db(self, error):
                    try:
                        self._do_it()
                    except Exception:  # fail-soft-ok
                        logger.debug("oh well")
        ''')
        assert "suppression-without-reason" in rules, out


class TestTheRealTreeStaysClean:
    """The adoption gate. Every boundary in `backend/` is either propagating or
    carries a written justification; this keeps it that way."""

    @pytest.mark.parametrize("target", ["backend"])
    def test_backend_reports_no_defects(self, target):
        r = subprocess.run([sys.executable, LINT, os.path.join(REPO, target)],
                           capture_output=True, text=True, cwd=REPO)
        assert r.returncode == 0, (
            "new swallowed failure(s) in %s:\n%s" % (target, r.stdout))

    def test_every_suppression_has_a_reason(self):
        r = subprocess.run(
            [sys.executable, LINT, "--list-suppressions",
             os.path.join(REPO, "backend")],
            capture_output=True, text=True, cwd=REPO)
        assert "(NO REASON GIVEN)" not in r.stdout, r.stdout
