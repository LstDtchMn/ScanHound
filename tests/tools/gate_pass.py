"""A COMPLETE recorded promotion-gate pass, for tests of flows behind R-6.

Merging this into a test config is the explicit statement 'this test assumes
the programme gate is open' -- the boundary tests in test_capability_gate.py
assert the opposite default."""
from backend.capability_gate import (
    BINDING_KEYS, PHASE_A_VERDICT_KEY, PHASE_B_VERDICT_KEY)
from backend.release_grammar import GRAMMAR_VERSION


def full_pass_config():
    bindings = {field: f"digest-{field}" for field in BINDING_KEYS}
    cfg = {key: bindings[field] for field, key in BINDING_KEYS.items()}
    verdict_bindings = dict(bindings, parser_version=GRAMMAR_VERSION)
    cfg[PHASE_A_VERDICT_KEY] = dict(verdict_bindings, acquisition_ready=True)
    cfg[PHASE_B_VERDICT_KEY] = dict(
        verdict_bindings, phase_b_status="pass",
        material_mismatch_count=0, inconclusive_count=0)
    return cfg
