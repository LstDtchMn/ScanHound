"""Repository gates for the HDEncode traffic-policy boundary."""
from pathlib import Path


_FORBIDDEN_AUTOMATION_OPTIONS = (
    "--disable-blink-features=AutomationControlled",
    "excludeSwitches",
    "useAutomationExtension",
)

# PR C owns only HDEncode listing, detail, and Selenium traffic. Other source
# clients (for example Rotten Tomatoes metadata) deliberately remain outside
# this policy and must not be forced through the HDEncode coordinator.
_HDENCODE_TRAFFIC_FILES = {
    "backend/hdencode_coordinator.py",
    "backend/hdencode_transport.py",
    "backend/scanner_service.py",
    "backend/detail_scraper.py",
    "backend/download_service.py",
    "backend/sources/hdencode.py",
    "backend/api/routes/sources.py",
}
_RAW_CONSTRUCTOR_OWNERS = {
    "cloudscraper.create_scraper(": {"backend/hdencode_transport.py"},
    "webdriver.Chrome(": {"backend/download_service.py"},
}


def test_no_browser_undetection_options_remain():
    violations = []
    for path in Path("backend").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in _FORBIDDEN_AUTOMATION_OPTIONS:
            if token in text:
                violations.append(f"{path.as_posix()}: {token}")
    assert violations == []


def test_raw_hdencode_constructors_are_confined_to_owned_factories():
    missing = sorted(
        rel for rel in _HDENCODE_TRAFFIC_FILES if not Path(rel).is_file()
    )
    assert missing == [], f"HDEncode policy files missing: {missing}"

    violations = []
    for token, allowed_files in _RAW_CONSTRUCTOR_OWNERS.items():
        for rel in sorted(_HDENCODE_TRAFFIC_FILES):
            text = Path(rel).read_text(encoding="utf-8")
            if token in text and rel not in allowed_files:
                violations.append(f"{rel}: {token}")
    assert violations == []


def test_constructor_gate_scope_is_hdencode_specific():
    # This is an explicit contract guard against accidentally expanding PR C
    # into unrelated metadata/source clients in a future edit.
    assert "backend/rt_scraper.py" not in _HDENCODE_TRAFFIC_FILES
    assert all("hdencode" in rel or rel in {
        "backend/scanner_service.py",
        "backend/detail_scraper.py",
        "backend/download_service.py",
        "backend/api/routes/sources.py",
    } for rel in _HDENCODE_TRAFFIC_FILES)
