"""Discriminatory-power check for the R4-94-1 fix (rescan verdict carrying).

For each mutation: reintroduce a defect, run the tests that are supposed to
catch it, and require that they FAIL. A test that passes under both the correct
and the defective implementation has no discriminatory power and is worse than
no test.

Companion to tests/tools/mutation_check.py, which does the same for the four
earlier review fixes. This one edits BY LINE NUMBER rather than by snippet:
`self.foo` and a bare `foo` are a substring trap, and a string-keyed mutation
that silently matches nothing reports "survived" for a test that is fine. Every
edit prints the line it replaced, so a shifted line number is visible in the
log rather than passing as a result.

Run with no arguments for all mutants, or name specific ones:

    python tests/tools/r4_94_1_mutation_check.py M1_restore_the_original_defect

Line numbers are literal and WILL drift. When they do, the printed "was:" line
is the check: it must show the code the mutation is meant to replace.
"""
import subprocess
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[2])
SVC = str(Path(ROOT) / "backend" / "scanner_service.py")
RTE = str(Path(ROOT) / "backend" / "api" / "routes" / "scanner.py")

TESTS = [
    "tests/test_rescan_carries_the_media_type_verdict.py",
    "tests/test_scanner_carries_is_tv.py",
    "tests/test_rescan_preserves_classification.py",
    "tests/test_api_routes.py",
    "tests/test_scan_rescan_item.py",
]

# name -> list of (path, 1-based line, replacement text)
MUTANTS = {
    # THE DEFECT ITSELF: the pre-fix composition, route evidence + fresh
    # detail only, with the carried cache evidence dropped.
    "M1_restore_the_original_defect": [
        (RTE, 517, "    verdict = resolve_listing_media_type("),
        (RTE, 518, "        {'type': {'4k': 'movie', 'remux': 'movie', 'tv': 'tv'}.get(details['category']),"
                   " 'title': existing.get('title') or ''}, details)"),
        (RTE, 20, "from backend.scanner_service import resolve_listing_media_type"),
    ],
    "M2_drop_cached_is_tv_evidence": [(SVC, 2176, "        if False else None,\n")],
    "M3_drop_cached_season_evidence": [(SVC, 2166, "        if False else None,\n")],
    "M4_drop_cached_category_evidence": [(SVC, 2161, "        if False else None,\n")],
    "M5_do_not_carry_the_stored_verdict": [
        (SVC, 2219, "    if False:\n")],
    "M6_stored_verdict_always_detail_authority": [
        (SVC, 2198, "    authority = (grammar.Authority.DETAIL if (provisional is None or provisional)\n")],
    "M7a_ambiguous_counts_as_a_movie_verdict": [
        (SVC, 2195, "    if stored not in ('tv', 'movie', 'ambiguous'):\n"),
        (SVC, 2201, "        grammar.MediaType.TV if stored == 'tv' else grammar.MediaType.MOVIE,\n")],
    "M7b_ambiguous_counts_as_a_tv_verdict": [
        (SVC, 2195, "    if stored not in ('tv', 'movie', 'ambiguous'):\n"),
        (SVC, 2201, "        grammar.MediaType.MOVIE if stored == 'movie' else grammar.MediaType.TV,\n")],
    "M8_conflict_no_longer_suppresses_the_route": [
        (SVC, 2142, "    category = ('' if False\n")],
    "M9_drop_the_fresh_detail_evidence": [(SVC, 2251, "        if False else None)\n")],
    "M10_answer_tv_unconditionally": [
        (SVC, 2252, "    return grammar.resolve_media_type([grammar.TypeEvidence(\n"
                    "        grammar.MediaType.TV, grammar.Authority.DETAIL, 'mutant')])\n")],
    "M11_answer_movie_unconditionally": [
        (SVC, 2252, "    return grammar.resolve_media_type([grammar.TypeEvidence(\n"
                    "        grammar.MediaType.MOVIE, grammar.Authority.DETAIL, 'mutant')])\n")],
    "M12_route_does_not_persist_the_verdict": [
        (RTE, 519, "    details['media_type_verdict'] = 'movie'\n")],
    "M13_drop_the_listing_title_fallback": [(SVC, 2241, "    if False:\n")],
}


def run():
    p = subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, text=True)
    tail = [ln for ln in p.stdout.strip().split("\n") if "passed" in ln or "failed" in ln or "error" in ln]
    failed = set()
    for ln in p.stdout.split("\n"):
        if ln.startswith("FAILED ") or ln.startswith("ERROR "):
            failed.add(ln.split(" ")[1].split(" - ")[0])
    return (tail[-1] if tail else "??"), failed


def main():
    sys.stdout.reconfigure(line_buffering=True)
    orig = {SVC: open(SVC, encoding="utf-8").read(),
            RTE: open(RTE, encoding="utf-8").read()}
    base_line, base_failed = run()
    print("BASELINE:", base_line)
    if base_failed:
        print("  baseline failures:", sorted(base_failed))
    results = {}
    only = sys.argv[1:]
    for name, edits in MUTANTS.items():
        if only and name not in only:
            continue
        try:
            for path in (SVC, RTE):
                open(path, "w", encoding="utf-8").write(orig[path])
            for path, lineno, text in edits:
                lines = open(path, encoding="utf-8").read().split("\n")
                # split("\n") -> index lineno-1 is the 1-based line
                print(f"  [{name}] {Path(path).name}:{lineno} was: {lines[lineno-1]!r}")
                lines[lineno - 1] = text.rstrip("\n")
                open(path, "w", encoding="utf-8").write("\n".join(lines))
            line, failed = run()
            killers = sorted(failed - base_failed)
            results[name] = (line, killers)
            print(f"{name}: {line}")
            for k in killers:
                print("    KILLED BY", k)
        finally:
            for path in (SVC, RTE):
                open(path, "w", encoding="utf-8").write(orig[path])
    after, _ = run()
    print("RESTORED:", after)


main()
