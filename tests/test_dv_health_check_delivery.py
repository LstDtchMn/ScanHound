"""Delivery-visibility tests for scripts/host-detector/dv_health_check.py.

DEFECT (DV-2): every push from the watchdog has been rejected by Gotify with
HTTP 401 since ~2026-08-21, and notify() hid it -- it truncated the push
container's captured output to 120 chars, so the log showed a chopped
traceback 218 times and never the status code or response body. log() also
wrote no timestamp, so the 218 lines could not even be dated.

These tests drive notify() with subprocess.run patched (no docker, no
network) and inspect what actually lands in the log file.
"""
import re
import sys
from pathlib import Path

# Resolve relative to THIS repo checkout (not a hardcoded path to the primary
# checkout) so the test exercises whichever worktree it is run from.
_HOST_DETECTOR_DIR = Path(__file__).resolve().parent.parent / "scripts" / "host-detector"
sys.path.insert(0, str(_HOST_DETECTOR_DIR))
import dv_health_check as H  # noqa: E402

ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ")


class _FakeCompletedProcess:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def _patch_gotify_token(monkeypatch, token="dummy-token"):
    monkeypatch.setattr(H, "_gotify_token", lambda: token)


def _read_log(tmp_path_logfile):
    return tmp_path_logfile.read_text(encoding="utf-8")


def _isolate_state(monkeypatch, tmp_path):
    logfile = tmp_path / "dv-health-check.log"
    monkeypatch.setattr(H, "LOGFILE", logfile)
    return logfile


def test_log_timestamps_every_line(monkeypatch, tmp_path):
    logfile = _isolate_state(monkeypatch, tmp_path)
    H.log("line one\nline two\nline three")
    content = _read_log(logfile)
    lines = [l for l in content.splitlines() if l]
    assert len(lines) == 3
    for line in lines:
        assert ISO_UTC_RE.match(line), "line missing ISO UTC timestamp: %r" % line
    assert lines[0].endswith("line one")
    assert lines[1].endswith("line two")
    assert lines[2].endswith("line three")


def test_401_body_is_logged_in_full_and_reported_undelivered(monkeypatch, tmp_path):
    logfile = _isolate_state(monkeypatch, tmp_path)
    _patch_gotify_token(monkeypatch)

    body = "you need to provide a valid access token"
    fake_output = "HTTP_ERROR 401\n%s\n" % body

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess(returncode=1, stdout=fake_output)

    monkeypatch.setattr(H.subprocess, "run", fake_run)

    delivered = H.notify("title", "message", 8)

    assert delivered is False, "a 401 must be reported as NOT delivered"
    content = _read_log(logfile)
    assert "401" in content
    assert body in content, "the actual response body must reach the log, not a truncated head"
    assert "ACTION" in content
    assert "own valid application token" in content


def test_long_failure_output_is_not_truncated_to_120_chars(monkeypatch, tmp_path):
    logfile = _isolate_state(monkeypatch, tmp_path)
    _patch_gotify_token(monkeypatch)

    # A realistic uncaught-exception dump: many times longer than the old
    # 120-char cap, so this proves the cap itself was raised, not just moved.
    marker = "DIAGNOSTIC-TAIL-MARKER"
    fake_output = ("Traceback (most recent call last):\n" * 20) + marker + "\n"

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess(returncode=1, stdout=fake_output)

    monkeypatch.setattr(H.subprocess, "run", fake_run)

    delivered = H.notify("title", "message", 8)

    assert delivered is False
    content = _read_log(logfile)
    assert marker in content, "output beyond the old 120-char cutoff must still reach the log"


def test_success_is_still_delivered_and_logged(monkeypatch, tmp_path):
    logfile = _isolate_state(monkeypatch, tmp_path)
    _patch_gotify_token(monkeypatch)

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess(returncode=0, stdout="HTTP_OK 200\n")

    monkeypatch.setattr(H.subprocess, "run", fake_run)

    delivered = H.notify("title", "message", 8)
    assert delivered is True
    content = _read_log(logfile)
    assert "delivered" in content
    assert ISO_UTC_RE.match(content.splitlines()[0])


def test_non_2xx_other_than_401_is_also_undelivered(monkeypatch, tmp_path):
    _isolate_state(monkeypatch, tmp_path)
    _patch_gotify_token(monkeypatch)

    def fake_run(*args, **kwargs):
        return _FakeCompletedProcess(returncode=1, stdout="HTTP_ERROR 500\nserver error\n")

    monkeypatch.setattr(H.subprocess, "run", fake_run)

    assert H.notify("title", "message", 8) is False


def test_token_override_file_is_honoured(monkeypatch, tmp_path):
    _isolate_state(monkeypatch, tmp_path)

    token_file = tmp_path / "gotify_token.txt"
    token_file.write_text("override-token-value\n", encoding="utf-8")
    monkeypatch.setenv(H.GOTIFY_TOKEN_FILE_ENV, str(token_file))

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompletedProcess(returncode=0, stdout="HTTP_OK 200\n")

    monkeypatch.setattr(H.subprocess, "run", fake_run)

    delivered = H.notify("title", "message", 8)

    assert delivered is True
    # The token is the last positional arg passed to the push container.
    assert captured["cmd"][-1] == "override-token-value"


def test_token_override_wins_over_compose_scrape(monkeypatch, tmp_path):
    _isolate_state(monkeypatch, tmp_path)

    token_file = tmp_path / "gotify_token.txt"
    token_file.write_text("from-override-file", encoding="utf-8")
    monkeypatch.setenv(H.GOTIFY_TOKEN_FILE_ENV, str(token_file))

    class _ExplodingCompose:
        def read_text(self, *a, **k):
            raise AssertionError(
                "compose file should not be read when the override is set")

    monkeypatch.setattr(H, "WUD_COMPOSE", _ExplodingCompose())

    assert H._gotify_token() == "from-override-file"


def test_missing_override_file_falls_back_to_compose_scrape(monkeypatch, tmp_path):
    _isolate_state(monkeypatch, tmp_path)
    monkeypatch.setenv(H.GOTIFY_TOKEN_FILE_ENV, str(tmp_path / "does-not-exist.txt"))

    class _FakeCompose:
        def read_text(self, *a, **k):
            return "WUD_TRIGGER_GOTIFY_MYGOTIFY_TOKEN=fallback-token\n"

    monkeypatch.setattr(H, "WUD_COMPOSE", _FakeCompose())

    assert H._gotify_token() == "fallback-token"


def test_no_token_at_all_is_not_delivered_and_logged(monkeypatch, tmp_path):
    logfile = _isolate_state(monkeypatch, tmp_path)
    monkeypatch.setattr(H, "_gotify_token", lambda: None)

    assert H.notify("title", "message", 8) is False
    content = _read_log(logfile)
    assert "no gotify token available" in content


# --- round-7 verifier: the override file as PowerShell and Notepad write it ---

def test_token_file_written_as_utf16_by_powershell_is_read(monkeypatch, tmp_path):
    """Out-File and '>' default to UTF-16 with a BOM. That used to raise
    UnicodeDecodeError out of _gotify_token() and kill the check."""
    _isolate_state(monkeypatch, tmp_path)
    token_file = tmp_path / "gotify.token"
    # Python's utf-16 codec writes the BOM itself, exactly as PowerShell does.
    token_file.write_bytes("AbCdEf123\r\n".encode("utf-16"))
    monkeypatch.setenv(H.GOTIFY_TOKEN_FILE_ENV, str(token_file))
    assert H._gotify_token() == "AbCdEf123"


def test_token_file_with_a_utf8_bom_does_not_keep_the_bom_in_the_token(monkeypatch, tmp_path):
    _isolate_state(monkeypatch, tmp_path)
    token_file = tmp_path / "gotify.token"
    token_file.write_bytes(b"\xef\xbb\xbfAbCdEf123\n")
    monkeypatch.setenv(H.GOTIFY_TOKEN_FILE_ENV, str(token_file))
    assert H._gotify_token() == "AbCdEf123"


def test_an_undecodable_token_file_falls_back_instead_of_crashing(monkeypatch, tmp_path):
    logfile = _isolate_state(monkeypatch, tmp_path)
    token_file = tmp_path / "gotify.token"
    token_file.write_bytes(b"\xff\x00\xfe\x80\x81 not text \xc3\x28")
    monkeypatch.setenv(H.GOTIFY_TOKEN_FILE_ENV, str(token_file))
    monkeypatch.setattr(H, "WUD_COMPOSE", tmp_path / "missing-compose.yml")
    assert H._gotify_token() is None          # fell back; the fallback has nothing either
    text = logfile.read_text(encoding="utf-8")
    assert "unreadable" in text and "UnicodeDecodeError" in text
    assert "not text" not in text, "the file's bytes must not be echoed into the log"
