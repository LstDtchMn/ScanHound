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
    "tests/test_rescan_conflict_suppression_is_order_independent.py",
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
        (RTE, 546, "    verdict = resolve_listing_media_type("),
        (RTE, 547, "        {'type': {'4k': 'movie', 'remux': 'movie', 'tv': 'tv'}.get(details['category']),"
                   " 'title': existing.get('title') or ''}, details)"),
        (RTE, 20, "from backend.scanner_service import resolve_listing_media_type"),
    ],
    "M2_drop_cached_is_tv_evidence": [(SVC, 2241, "        if False else None,\n")],
    "M3_drop_cached_season_evidence": [(SVC, 2224, "        if False else None,\n")],
    "M4_drop_cached_category_evidence": [(SVC, 2219, "        if False else None,\n")],
    "M5_do_not_carry_the_stored_verdict": [
        (SVC, 2354, "    if False:\n")],
    "M6_stored_verdict_always_detail_authority": [
        (SVC, 2315, "    authority = (grammar.Authority.DETAIL if (provisional is None or provisional)\n")],
    "M7a_ambiguous_counts_as_a_movie_verdict": [
        (SVC, 2310, "    if stored not in ('tv', 'movie', 'ambiguous'):\n"),
        (SVC, 2318, "        grammar.MediaType.TV if stored == 'tv' else grammar.MediaType.MOVIE,\n")],
    "M7b_ambiguous_counts_as_a_tv_verdict": [
        (SVC, 2310, "    if stored not in ('tv', 'movie', 'ambiguous'):\n"),
        (SVC, 2318, "        grammar.MediaType.MOVIE if stored == 'movie' else grammar.MediaType.TV,\n")],
    "M8_conflict_no_longer_suppresses_the_route": [
        (SVC, 2193, "    category = ('' if False\n")],
    "M9_drop_the_fresh_detail_evidence": [(SVC, 2386, "        if False else None)\n")],
    "M10_answer_tv_unconditionally": [
        (SVC, 2387, "    return grammar.resolve_media_type([grammar.TypeEvidence(\n"
                    "        grammar.MediaType.TV, grammar.Authority.DETAIL, 'mutant')])\n")],
    "M11_answer_movie_unconditionally": [
        (SVC, 2387, "    return grammar.resolve_media_type([grammar.TypeEvidence(\n"
                    "        grammar.MediaType.MOVIE, grammar.Authority.DETAIL, 'mutant')])\n")],
    "M12_route_does_not_persist_the_verdict": [
        (RTE, 548, "    details['media_type_verdict'] = 'movie'\n")],
    "M13_drop_the_listing_title_fallback": [(SVC, 2376, "    if False:\n")],

    # ── R4-94-2: the feedback loop, and the shadow field ────────────────────
    #
    # M14 is THE FINDING. It restores the state at c5a5ab4: cached is_tv
    # admitted at DETAIL on every row, including the rows whose is_tv the route
    # itself wrote. Two rescans with nothing new observed then clear the
    # provisional flag that gates autonomous action.
    "M14_readmit_is_tv_on_current_format_rows": [
        (SVC, 2201, "    legacy_row = True\n")],
    # The other side of the same rule: never admit it. Legacy rows written by
    # main since #93 carry their decision in that boolean alone.
    "M15_never_admit_cached_is_tv": [
        (SVC, 2201, "    legacy_row = False\n")],
    # 'ambiguous' read as "no verdict recorded", so a row that decided nothing
    # counts as legacy and its is_tv shadow is admitted again -- the L2 half of
    # the loop on its own.
    "M16_ambiguous_is_not_a_recorded_verdict": [
        (SVC, 2175, "    return stored if stored in ('tv', 'movie') else ''\n")],
    # The route's legacy field goes back to being an OR beside the verdict,
    # free to contradict it.
    "M17_route_ors_the_legacy_is_tv_beside_the_verdict": [
        (RTE, 571, "        'is_tv': (verdict.media_type is grammar.MediaType.TV\n"
                   "                  or details.get('is_tv', False)\n"
                   "                  or (details['category'] == 'tv'\n"
                   "                      or bool(cached_row.get('is_tv'))\n"
                   "                      or cached_row.get('season') is not None)),\n")],
    # The shadow inverted: is_tv True exactly when the verdict is NOT tv. A
    # crude mutant, but it is the one that proves the contradiction assertion
    # reads BOTH fields rather than just asserting the verdict twice.
    "M18_route_inverts_the_shadow": [
        (RTE, 571, "        'is_tv': verdict.media_type is not grammar.MediaType.TV,\n")],

    # ── R4-94-3: order-dependent suppression, and its three siblings ────────
    #
    # M0 is the whole finding set at once: with these four edits the tree
    # BEHAVES exactly as 1965399 does. It is here so the claim "this reproduces
    # at the reviewed head" is executable rather than asserted -- the R4-94-3
    # probe under M0 prints the 1965399 column of the commit message.
    "M0_restore_all_four_R4_94_3_defects": [
        (RTE, 114, '    attested = False\n'),
        (SVC, 2354, "    if stored:\n"),
        (SVC, 2312, "    if False:\n"),
        (SVC, 1555, "                is_tv=(bool(d['is_tv']) if 'is_tv' in d\n"
                    "                       else d.get('season') is not None),\n"),
    ],
    # C1 ITSELF. cached_verdict_evidence stops consulting the conflict, so a
    # stored PROVISIONAL verdict -- the suppressed route's own answer --
    # re-enters at ROUTE authority above the suppression that removed it.
    "M19_conflict_does_not_suppress_the_stored_verdict": [
        (SVC, 2312, "    if False:\n")],
    # C2. cached_media_type carries a conflicted row's stored verdict verbatim,
    # which is the state rev3.8's "already refused the same row" described.
    "M20_conflicted_stored_verdict_carried_verbatim": [
        (SVC, 2354, "    if stored:\n")],
    # C3. The sibling cache->item reader goes back to carrying is_tv verbatim
    # while setting media_type independently.
    "M21_reader_carries_is_tv_verbatim": [
        (SVC, 1555, "                is_tv=(bool(d['is_tv']) if 'is_tv' in d\n"
                    "                       else d.get('season') is not None),\n")],
    # C4. The rescan path drops the crawl's attestation again.
    "M22_rescan_drops_the_attestation": [
        (RTE, 114, '    attested = False\n')],
    # OVER-SUPPRESSION, the opposite error. A DECIDED verdict had
    # TITLE-or-better evidence behind it and a cross-listing conflict says
    # nothing about that; suppressing it too would make a rescan refuse rows it
    # has every right to decide.
    "M23_suppression_also_removes_a_decided_verdict": [
        (SVC, 2288, "    return True\n")],
    # The suppression keys on the PROVISIONAL flag alone and forgets the
    # conflict, so every provisional verdict anywhere is thrown away. Proves the
    # C1/C2 assertions are not satisfied by any rule that merely distrusts
    # provisional verdicts.
    "M24_suppression_ignores_the_conflict": [
        (SVC, 2285, "    if False:\n")],
    # OVER-REACH in the other direction: the conflict blanks the recorded
    # SEASON as well as the route. A conflict is about which listing carried
    # the release, not about what the filename said.
    "M25_conflict_also_suppresses_the_recorded_season": [
        (SVC, 2224, "        if cached.get('season') is not None"
                    " and not cached.get('category_conflict') else None,\n")],
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
