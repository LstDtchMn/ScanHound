"""Red-first tests for the two pure gate functions in collect_shadow_evidence.

These exist because both defects this gate has produced were invisible to a code
read. The first (2026-07-22 -> 2026-08-05) read `ready_matches` out of the wrong
dictionary and failed the window for 14.9 days. The second stopped the window on
a raw miss count, which counts "the feed caught up 40 minutes later" as
permanent coverage loss.

The discrimination requirement: each test picks inputs where the correct rule
and the plausible-wrong rule DISAGREE. A test that passes under `if raw_misses:`
would prove nothing, so the live-data case below is asserted to NOT stop --
that single assertion fails against the old implementation.

Run:  python test_gate_conditions.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect_shadow_evidence import miss_stop_conditions, reconciliation_blockers

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def verdict(green=0, yellow=0, red=0, pending=0, ambiguous=0):
    return {
        "green": green, "yellow": yellow, "red": red,
        "pending": pending, "ambiguous": ambiguous,
        "total": green + yellow + red + pending + ambiguous,
        "green_hours": 6.0, "yellow_hours": 24.0,
        "ambiguous_urls": ["u"] * ambiguous, "red_urls": ["u"] * red,
    }


print("miss_stop_conditions -- what must NOT stop the window")

# The live 2026-08-05 measurement. 150 raw misses, every one of them provably
# resolved within 6h except one that cannot be proven either way. This is the
# discriminating case: the old `if raw_misses:` rule stops here, the tiered rule
# stops only for the 1 ambiguous. Asserted separately below.
live = verdict(green=149, ambiguous=1)
stop = miss_stop_conditions(live, raw_misses=150)
check("live data stops for the ambiguous one only",
      len(stop) == 1 and "UNPROVABLE" in stop[0], stop)
check("live data does NOT cite the raw count of 150",
      not any("150" in s for s in stop), stop)

# GREEN is the normal case: acquired inside 6h. Never a stop.
check("all-GREEN does not stop",
      miss_stop_conditions(verdict(green=200), raw_misses=200) == [])

# YELLOW is 6-24h. Jesse's criterion calls it acceptable. If YELLOW stopped the
# window, defining the tier would be pointless -- this is the assertion that
# pins the tier as load-bearing rather than decorative.
check("YELLOW alone does not stop",
      miss_stop_conditions(verdict(green=10, yellow=5), raw_misses=15) == [])

print("miss_stop_conditions -- what MUST stop the window")

# The whole risk of this change is silencing a real miss. One RED among any
# number of GREENs must still stop it.
stop = miss_stop_conditions(verdict(green=999, red=1), raw_misses=1000)
check("a single RED among 999 GREEN still stops",
      len(stop) == 1 and "NEVER RESOLVED" in stop[0], stop)

stop = miss_stop_conditions(verdict(green=10, pending=3), raw_misses=13)
check("PENDING stops", any("STILL UNRESOLVED" in s for s in stop), stop)

stop = miss_stop_conditions(verdict(red=2, pending=1, ambiguous=4), raw_misses=7)
check("all three failing classes are reported, not just the first",
      len(stop) == 3, stop)
check("counts are carried through to the message",
      any("x2" in s for s in stop) and any("x4" in s for s in stop), stop)

# Fail closed. An absent grading must never read as "no bad misses" -- that is
# the same failure mode as the wrong-dictionary bug, where a missing key read as
# a pass.
stop = miss_stop_conditions(None, raw_misses=150)
check("grading unavailable fails CLOSED",
      len(stop) == 1 and "UNAVAILABLE" in stop[0], stop)
check("the unavailable message still reports the raw count for triage",
      "150" in stop[0], stop)
check("grading unavailable with ZERO raw misses still fails closed",
      miss_stop_conditions(None, raw_misses=0) != [])
check("malformed grading fails closed",
      miss_stop_conditions("149 green", raw_misses=150) != [])
check("empty-dict grading fails closed on nothing -- it is a real verdict",
      miss_stop_conditions({}, raw_misses=0) == [])

# Guard the coercion: a JSON null or string count must not crash the collector
# mid-window, and must not silently pass either.
check("string counts are coerced, not crashed on",
      miss_stop_conditions({"red": "2"}, raw_misses=2) != [])
check("null counts read as zero",
      miss_stop_conditions({"red": None, "green": 5}, raw_misses=5) == [])

print("reconciliation_blockers -- regression pins for the wrong-dictionary bug")

READY = {"ready": True, "checks": {}}

# The exact live values that the old code read as a failure for 14.9 days.
check("real reconciliation passes",
      reconciliation_blockers(
          READY, missing_credentials=[],
          reconciliation={"ready_matches": True, "cycles_delta": 0,
                          "misses_delta": 0}) == [])

# The bug: the comparison lives in `reconciliation`, and app_readiness never
# carries a ready_matches key. Absent must fail, not pass.
check("missing reconciliation fails closed",
      reconciliation_blockers(READY, missing_credentials=[],
                              reconciliation=None) != [])
check("ready_matches False fails",
      reconciliation_blockers(
          READY, missing_credentials=[],
          reconciliation={"ready_matches": False}) != [])
check("ready_matches absent from a present dict fails closed",
      reconciliation_blockers(READY, missing_credentials=[],
                              reconciliation={"cycles_delta": 0}) != [])
check("readiness error fails even when reconciliation agrees",
      reconciliation_blockers(
          {"error": "connection refused"}, missing_credentials=[],
          reconciliation={"ready_matches": True}) != [])
check("missing credentials fail",
      reconciliation_blockers(READY, missing_credentials=["auth-token.txt"],
                              reconciliation={"ready_matches": True}) != [])

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)}: {FAILURES}")
    sys.exit(1)
print("all gate-condition tests passed")
