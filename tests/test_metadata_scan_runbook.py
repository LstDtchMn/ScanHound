from pathlib import Path


RUNBOOK = Path("docs/feature-pack-review/4K_METADATA_PILOT_AND_FULL_SCAN_RUNBOOK.md")


def test_runbook_requires_pilot_before_full_scan_and_keeps_writes_gated():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "Pilot acceptance" in text
    assert "Auto-rename remains disabled" in text
    assert "Plex label dry run" in text
    assert "Do not run Kometa" in text
    assert "25–50" in text


def test_kometa_badges_cover_the_closed_managed_label_set():
    text = Path("docs/kometa/dv_badges.yml").read_text(encoding="utf-8")
    for label in ("DV FEL", "DV MEL", "DV P8", "DV P5"):
        assert f"  {label}:" in text
        assert f"label: {label}" in text
