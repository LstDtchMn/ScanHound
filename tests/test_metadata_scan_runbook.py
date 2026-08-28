from pathlib import Path


RUNBOOK = Path("docs/feature-pack-review/4K_METADATA_PILOT_AND_FULL_SCAN_RUNBOOK.md")


def test_runbook_requires_pilot_before_full_scan_and_keeps_writes_gated():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "Pilot acceptance" in text
    assert "Auto-rename remains disabled" in text
    assert "Plex label dry run" in text
    assert "Do not run Kometa" in text
    assert "25–50" in text


def test_kometa_badges_cover_the_closed_layer_badge_set():
    """The design must keep covering the four LAYER badges.

    Renamed 2026-08-26: this said "managed label set", but dv_labeler.MANAGED is
    nine labels including DV7, DV, HDR10 and the retiring pair. These four are
    the layer-badge subset. Conflating them is what let an earlier change
    overwrite the design while believing it was faithful.

    The artifact moved to DV_BADGE_DESIGN.md -- deliberately out of .yml, since
    a YAML file under docs/kometa/ reads as something to drop into Kometa, and
    that appearance had already misled someone.
    """
    text = Path("docs/kometa/DV_BADGE_DESIGN.md").read_text(encoding="utf-8")
    for label in ("DV FEL", "DV MEL", "DV8", "DV5"):
        assert f"  {label}:" in text
        assert f"label: {label}" in text
