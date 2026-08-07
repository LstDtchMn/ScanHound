"""Structured outcome fields and queue routing must not drift apart.

THE BUG THIS EXISTS TO PREVENT, which has now happened twice.

1. 2026-08-06, reveal throttle. I built the fix, set
   `affected_scope="source"` and `retry_mode="after_cooldown"` on the
   diagnostic, and wrote eleven tests that all passed. But `download_queue`
   never reads those fields: it routes on `is_source_wide_denial()`, which
   requires the reason_code to be a member of `_SOURCE_WIDE_REASONS`. The new
   code was not in that set, so every field was decorative and the item would
   still have become terminally failed. Eleven green tests over a fix that did
   nothing.

2. Minutes later, rerouting the coordinator call broke eight tests -- and the
   failure surfaced as `SCRAPE_EXCEPTION`, because a missing method on a test
   double was swallowed by a broad handler. A real API mismatch would look like
   an ordinary scrape failure rather than a wiring error.

Both are the same shape: **a contract that nothing checks.** The peer review put
it directly -- "a future reason can again carry the correct structured fields and
do nothing if the set is not updated" -- and asked for an invariant.

WHY THIS IS A STATIC SCAN. A per-reason unit test only protects the reasons
somebody remembered to write a test for, which is exactly what failed. Reading
the AST covers every construction site in the module, including ones added later
by someone who never opens this file.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.download_outcome import _SOURCE_WIDE_REASONS
from backend.scrape_outcome import ScrapeCode

MODULES = ("backend/download_service.py", "backend/download_outcome.py")


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "backend" / "download_outcome.py").exists():
            return parent
    raise AssertionError("could not locate the repository root")


def _diagnostic_sites():
    """Every ScrapeDiagnostic(...) construction, with the fields we care about.

    Yields (module, lineno, code_name_or_None, fields). code_name is None when
    the first argument is not a literal `ScrapeCode.X` -- those are reported
    separately rather than silently ignored, since an invariant that quietly
    skips cases is the problem, not the solution.
    """
    root = _repo_root()
    for rel in MODULES:
        path = root / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "ScrapeDiagnostic":
                continue
            code = None
            if node.args:
                first = node.args[0]
                if (isinstance(first, ast.Attribute)
                        and getattr(first.value, "id", None) == "ScrapeCode"):
                    code = first.attr
            fields = {}
            for kw in node.keywords:
                if isinstance(kw.value, ast.Constant):
                    fields[kw.arg] = kw.value.value
                else:
                    fields[kw.arg] = "<dynamic>"
            yield rel, node.lineno, code, fields


def _value_of(code_name: str) -> str:
    return getattr(ScrapeCode, code_name).value


def test_there_are_diagnostic_sites_to_check():
    """Positive control. If the scan finds nothing, every assertion below passes
    vacuously -- which is precisely the failure mode this file guards against."""
    sites = list(_diagnostic_sites())
    assert len(sites) >= 10, f"scan found only {len(sites)} sites; is it broken?"


def test_every_source_scoped_outcome_is_recognised_by_the_queue():
    """THE NEAR-MISS. scope='source' with no set membership does nothing."""
    offenders = []
    for module, line, code, fields in _diagnostic_sites():
        if fields.get("affected_scope") != "source":
            continue
        if code is None:
            continue  # covered by the dynamic-site test below
        if _value_of(code) not in _SOURCE_WIDE_REASONS:
            offenders.append(f"{module}:{line} {code}")
    assert not offenders, (
        "these outcomes declare affected_scope='source' but their reason_code is "
        "NOT in _SOURCE_WIDE_REASONS, so is_source_wide_denial() returns False "
        "and download_queue sends them to _fail as terminal failures. The "
        "structured fields are decorative:\n  " + "\n  ".join(offenders))


def test_every_source_wide_reason_is_actually_constructed_as_source_scoped():
    """The reverse direction: set membership without the field is also a lie.

    A reason in the set whose construction omits affected_scope='source' will
    never satisfy is_source_wide_denial(), which checks BOTH.
    """
    constructed = {}
    for module, line, code, fields in _diagnostic_sites():
        if code is None:
            continue
        constructed.setdefault(_value_of(code), []).append(
            (f"{module}:{line}", fields.get("affected_scope")))

    offenders = []
    for reason in sorted(_SOURCE_WIDE_REASONS):
        sites = constructed.get(reason)
        if not sites:
            continue  # constructed elsewhere or only in tests; not a drift bug
        if not any(scope == "source" for _, scope in sites):
            offenders.append(f"{reason} at {[s for s, _ in sites]}")
    assert not offenders, (
        "these reasons are in _SOURCE_WIDE_REASONS but are never constructed "
        "with affected_scope='source', so the queue will not route them as "
        "source-wide:\n  " + "\n  ".join(offenders))


def test_after_cooldown_outcomes_carry_a_cooldown_and_source_scope():
    """retry_mode='after_cooldown' is meaningless without a cooldown to wait for
    and a source-wide route to pause the batch."""
    offenders = []
    for module, line, code, fields in _diagnostic_sites():
        if fields.get("retry_mode") != "after_cooldown":
            continue
        if fields.get("affected_scope") != "source":
            offenders.append(f"{module}:{line} {code} lacks source scope")
        if "cooldown_until" not in fields:
            offenders.append(f"{module}:{line} {code} sets no cooldown_until")
    assert not offenders, "\n  ".join(offenders)


def test_dynamic_reason_sites_are_known_and_few():
    """A construction whose reason is a variable cannot be checked statically.

    Rather than skip them silently, pin the count. If someone adds another, this
    fails and forces a decision: make it literal, or cover it with a runtime
    test.
    """
    dynamic = [f"{m}:{l}" for m, l, code, _ in _diagnostic_sites() if code is None]
    assert len(dynamic) <= 1, (
        "new ScrapeDiagnostic sites use a non-literal reason code and are "
        "therefore invisible to this invariant:\n  " + "\n  ".join(dynamic))


def test_the_reveal_stall_reason_specifically_still_routes():
    """A named regression guard for the reason that caused all of this.

    Deliberately redundant with the scan above: if someone weakens the scan,
    this still fails.
    """
    assert ScrapeCode.REVEAL_VERIFICATION_STALLED.value in _SOURCE_WIDE_REASONS


@pytest.mark.parametrize("reason", sorted(_SOURCE_WIDE_REASONS))
def test_source_wide_reasons_are_real_scrape_codes(reason):
    """A typo in the set would silently never match anything."""
    assert reason in {c.value for c in ScrapeCode}, (
        f"{reason!r} is in _SOURCE_WIDE_REASONS but is not a ScrapeCode value, "
        "so it can never match an outcome")
