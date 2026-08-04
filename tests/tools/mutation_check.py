"""Discriminatory-power check for the four review fixes.

For each fix: restore the OLD (defective) implementation, run the regression
tests that are supposed to catch it, and require that they FAIL. Then restore
the corrected code and require that they PASS.

A test that passes under both implementations has no discriminatory power and
is worse than no test — it buys false confidence. This proves each new test has
it. Run inside the test container against /app.
"""
import subprocess
import sys
from pathlib import Path

#: Repository root, derived from this file so the harness runs anywhere — a
#: throwaway container, a developer checkout, or a CI runner. It was hardcoded
#: to /app, which silently limited it to the one environment it was written in.
APP = Path(__file__).resolve().parents[2]

# (label, file, corrected_snippet, defective_snippet, tests_that_must_fail)
MUTATIONS = [
    (
        "round-11 F1 -- demotion must propagate the effective mode",
        "backend/background_scanner.py",
        '                if rss_cycle and rss_cycle.get("mode"):\n'
        '                    discovery_mode = rss_cycle["mode"]',
        "                pass  # MUTATION: propagation disabled",
        ["tests/test_background_scanner.py::TestR6DemotionRestoresTheSafetyNet::test_demoted_primary_runs_listing_and_shadow"],
    ),
    (
        "blocker 1 — coverage keyed off colour instead of first_normal_at",
        "backend/sweep/gate.py",
        "    elif first_normal is not None:",
        "    elif rss_state in (RssAcquisition.GREEN, RssAcquisition.YELLOW):",
        ["tests/test_sweep_gate.py::TestLagAwareness::test_YELLOW_without_first_normal_at_is_NOT_covered_by_rss",
         "tests/test_sweep_gate.py::TestLagAwareness::test_coverage_follows_the_observation_not_the_colour"],
    ),
    (
        "blocker 2 — completion used the oldest possible edge",
        "backend/sweep/completion.py",
        "        oldest = min(timed, key=lambda t: t.newest_possible)\n"
        "        if oldest.newest_possible <= stop_target:",
        "        oldest = min(timed, key=lambda t: t.earliest_possible)\n"
        "        if oldest.earliest_possible <= stop_target:",
        ["tests/test_sweep_completion.py::TestCompletion::test_the_whole_possible_interval_must_be_past_the_target"],
    ),
    (
        "blocker 3 — reconciliation conditional on a token existing",
        "docs/feature-pack-review/qualification-evidence/collect_shadow_evidence.py",
        "    if missing_credentials:\n"
        "        return [f\"NO AUTH TOKEN at {token_name} — the independent readiness \"\n"
        "                \"cross-check could not run; qualification requires it\"]",
        "    if missing_credentials:\n"
        "        return []",
        ["tests/test_collector_reconciliation.py::TestFailsClosed::test_no_auth_token_BLOCKS",
         "tests/test_collector_reconciliation.py::TestFailsClosed::test_no_auth_token_blocks_EVEN_WITH_a_passing_app_readiness"],
    ),
    (
        "blocker 5 — volume anomaly on by default with an invented constant",
        "backend/sweep/structure.py",
        "VOLUME_ANOMALY_ENABLED = False",
        "VOLUME_ANOMALY_ENABLED = True",
        ["tests/test_sweep_structure.py::TestVolumeAnomalyIsDisabledUntilCalibrated::test_a_volume_collapse_does_NOT_fire_by_default"],
    ),
]


def run(tests):
    p = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=APP, capture_output=True, text=True)
    return p.returncode == 0, p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""


def main():
    failures = []
    for label, rel, good, bad, tests in MUTATIONS:
        path = APP / rel
        original = path.read_text(encoding="utf-8")

        if good not in original:
            failures.append(f"{label}: SETUP ERROR — corrected snippet not found in {rel}")
            print(f"[SETUP FAIL] {label}")
            continue

        # 1. corrected code must PASS
        ok_fixed, line_fixed = run(tests)

        # 2. defective code must FAIL
        path.write_text(original.replace(good, bad, 1), encoding="utf-8")
        try:
            ok_broken, line_broken = run(tests)
        finally:
            path.write_text(original, encoding="utf-8")

        discriminates = ok_fixed and not ok_broken
        status = "DISCRIMINATES" if discriminates else "NO POWER"
        print(f"[{status}] {label}")
        print(f"          corrected -> {'PASS' if ok_fixed else 'FAIL'}   ({line_fixed})")
        print(f"          defective -> {'PASS' if ok_broken else 'FAIL'}   ({line_broken})")
        if not discriminates:
            failures.append(label)

    print()
    if failures:
        print(f"RESULT: {len(failures)} test(s) lack discriminatory power")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"RESULT: all {len(MUTATIONS)} regression tests fail on the old "
          f"implementation and pass on the corrected one")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
