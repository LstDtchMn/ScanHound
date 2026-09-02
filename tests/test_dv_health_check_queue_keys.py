"""The host watchdog keys each queue condition on its own (round-7 review, DLQ-2).

Until now all three queue conditions shared one marker key, "queue_stalled".
Once any of them had alerted, a later, different condition was logged as
"problems unchanged; already alerted" and never reached Gotify -- so after the
first hold-related message, a starving queue was silent. This drives the real
main() with the other subsystems stubbed healthy and a fake /health body, and
counts what notify() would have sent.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os

import pytest

HERE = os.path.dirname(__file__)
SCRIPT = os.path.abspath(os.path.join(HERE, "..", "scripts", "host-detector", "dv_health_check.py"))


def _load():
    spec = importlib.util.spec_from_file_location("dv_health_check_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def checker(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "MARKER", tmp_path / "marker")
    monkeypatch.setattr(mod, "LOGFILE", tmp_path / "check.log")
    monkeypatch.setattr(mod, "check_tools", lambda: [])
    monkeypatch.setattr(mod, "check_db", lambda: ([], {}))
    monkeypatch.setattr(mod, "check_jd", lambda: [])
    sent = []
    monkeypatch.setattr(mod, "notify", lambda title, message, priority=7: (sent.append((title, message)) or True))
    state = {"body": {}}
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda url, timeout=20: _Resp(json.dumps(state["body"]).encode("utf-8")))

    def run(**queue):
        ev = {"due_now": 1, "oldest_due_at": "2026-09-02T10:01:01+00:00", "last_attempt_at": None,
              "verification_holds": 1, "batches_deferred_without_auto_resume": 0,
              "last_source_progress_at": None, "progress_deadline_seconds": 900}
        state["body"] = {"status": "ok", "queue": dict(queue, evidence=ev)}
        before = len(sent)
        assert mod.main() == 0
        return sent[before:]

    return mod, run, sent


def test_a_new_queue_condition_alerts_even_while_an_older_one_is_active(checker):
    """The reviewer's three-run probe: this is what used to go silent."""
    _mod, run, _sent = checker
    first = run(human_required=True)
    assert len(first) == 1 and "needs a person" in first[0][1]

    second = run(human_required=True, source_no_progress=True)
    assert len(second) == 1, "source_no_progress appeared while human_required was active and did not alert"
    assert "not progressing" in second[0][1]

    third = run(human_required=True, source_no_progress=True, executor_starved=True)
    assert len(third) == 1, "executor_starved appeared while two other conditions were active and did not alert"
    assert "not picking up work" in third[0][1]

    fourth = run(human_required=True, source_no_progress=True, executor_starved=True)
    assert fourth == [], "nothing new, so nothing should be sent"


def test_each_condition_has_its_own_marker_key(checker):
    mod, run, _sent = checker
    run(human_required=True, executor_starved=True)
    assert mod._read_active_keys() == {"queue_human_required", "queue_starved"}


def test_a_condition_that_clears_and_returns_alerts_again(checker):
    mod, run, _sent = checker
    assert len(run(executor_starved=True, human_required=True)) == 1
    assert run(human_required=True) == []          # starvation cleared: nothing new to say
    assert mod._read_active_keys() == {"queue_human_required"}
    again = run(executor_starved=True, human_required=True)
    assert len(again) == 1 and "not picking up work" in again[0][1]


def test_the_title_names_the_conditions(checker):
    _mod, run, _sent = checker
    out = run(executor_starved=True)
    assert out[0][0] == "ScanHound: queue starved"


def test_check_queue_returns_one_entry_per_condition_in_report_order():
    mod = _load()
    body = {"queue": {"executor_starved": True, "source_no_progress": True, "human_required": True,
                      "evidence": {}}}
    out = mod.check_queue(body)
    assert list(out) == ["queue_starved", "queue_no_progress", "queue_human_required"]
    assert mod.check_queue({}) == {}
    assert mod.check_queue({"queue": "not a dict"}) == {}
