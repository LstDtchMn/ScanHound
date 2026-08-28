"""The Click'n'Load fallback AT THE CONSUMER: what actually gets archived.

The transport is tested in test_clicknload_transport.py. This file tests the
only thing that matters afterwards -- that a hand-off which CANNOT be confirmed
does not get written down as one that was.

That distinction is the whole reason the archive model exists: an item counts as
grabbed only when it truly reached JDownloader. A fallback that quietly reused
`status='completed'` would have been indistinguishable in the database from a
confirmed API delivery, and no later query could have separated them.

The dual requirement, and the reason this is not simply "mark it failed":

  * the row must NOT claim confirmation -- so a distinct status
  * the row MUST still suppress a re-grab -- the links DID reach JDownloader,
    and grabbing again would duplicate the download

All six grabbed-set queries in the codebase spell that as `!= 'failed'`, so the
second requirement is satisfied by construction. There is a test below that
pins it, because "by construction" is exactly the kind of claim that stops
being true when somebody adds a seventh query.
"""
from unittest.mock import MagicMock, patch

import pytest

from backend.clicknload import CnlResult
from backend.download_service import DownloadService

LINKS = ["https://rapidgator.net/file/aaa", "https://rapidgator.net/file/bbb"]


def _svc(**overrides):
    cfg = {
        "jd_enabled": True,
        "jd_method": "api",
        "jd_movies_folder": "F:\\Downloads",
        "jd_clicknload_fallback": True,
        "jd_clicknload_url": "http://jd.test:9666",
    }
    cfg.update(overrides)
    db = MagicMock()
    db.is_downloaded.return_value = False
    svc = DownloadService(config=cfg, db=db)
    svc.server_mode = True
    svc._log = MagicMock()
    return svc


class TestTheSendReportsWhichTransportCarriedTheLinks:
    """`outcome` is an out-parameter, so it is easy to add a return path and
    forget to fill it. Each path is asserted separately."""

    def test_a_successful_api_send_reports_confirmed(self):
        svc = _svc()
        device = MagicMock()
        with patch.object(svc, "_connect_jd_device", return_value=device):
            out = {}
            assert svc.send_to_jdownloader(LINKS, "Pkg", outcome=out) is True
        assert out == {"transport": "api", "confirmed": True}

    def test_the_folder_path_reports_UNconfirmed(self, tmp_path):
        """A .crawljob file proves the file exists, not that JDownloader read
        it. Same class of evidence as Click'n'Load, so it says so."""
        svc = _svc(jd_method="folder", jd_folder=str(tmp_path))
        out = {}
        assert svc.send_to_jdownloader(LINKS, "Pkg", outcome=out) is True
        assert out == {"transport": "folder", "confirmed": False}

    def test_the_fallback_reports_clicknload_and_UNconfirmed(self):
        svc = _svc()
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("cloud down")), \
             patch("backend.clicknload.probe",
                   return_value=CnlResult(accepted=True, http_status=200,
                                          body="jdownloader=true;")), \
             patch("backend.clicknload.add_links",
                   return_value=CnlResult(accepted=True, http_status=200)):
            out = {}
            assert svc.send_to_jdownloader(LINKS, "Pkg", outcome=out) is True
        assert out == {"transport": "clicknload", "confirmed": False}

    def test_omitting_outcome_still_works(self):
        """Caller 2 (hdencode_action_service) does not pass one."""
        svc = _svc()
        with patch.object(svc, "_connect_jd_device", return_value=MagicMock()):
            assert svc.send_to_jdownloader(LINKS, "Pkg") is True


class TestTheFallbackOnlyFiresWhenItShould:

    def test_it_is_not_reached_while_the_api_works(self):
        svc = _svc()
        with patch.object(svc, "_connect_jd_device", return_value=MagicMock()), \
             patch("backend.clicknload.add_links") as add:
            svc.send_to_jdownloader(LINKS, "Pkg")
        assert not add.called, (
            "the local fallback ran even though the cloud send succeeded")

    def test_it_can_be_switched_off(self):
        svc = _svc(jd_clicknload_fallback=False)
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("down")), \
             patch("backend.clicknload.probe") as probe:
            assert svc.send_to_jdownloader(LINKS, "Pkg") is False
        assert not probe.called

    def test_a_non_jdownloader_on_the_port_is_REFUSED(self):
        """The control that matters. If probe() were status-only, a proxy
        answering 200 would take the links and the log would record a
        successful delivery."""
        svc = _svc()
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("down")), \
             patch("backend.clicknload.probe",
                   return_value=CnlResult(accepted=False,
                                          error="not JDownloader")), \
             patch("backend.clicknload.add_links") as add:
            out = {}
            assert svc.send_to_jdownloader(LINKS, "Pkg", outcome=out) is False
        assert not add.called, "it handed links to something that is not JDownloader"
        assert out == {}

    def test_a_failed_hand_off_does_not_report_delivery(self):
        svc = _svc()
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("down")), \
             patch("backend.clicknload.probe",
                   return_value=CnlResult(accepted=True, body="jdownloader=true;")), \
             patch("backend.clicknload.add_links",
                   return_value=CnlResult(accepted=False, http_status=500)):
            out = {}
            assert svc.send_to_jdownloader(LINKS, "Pkg", outcome=out) is False
        assert out == {}

    def test_an_exception_in_the_fallback_never_escapes(self):
        """It runs where the primary send already failed. Raising here would
        replace a clean 'JD API error' with a traceback and bury the cause."""
        svc = _svc()
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("down")), \
             patch("backend.clicknload.probe", side_effect=OSError("boom")):
            assert svc.send_to_jdownloader(LINKS, "Pkg") is False


class TestWhatGetsWrittenToTheArchive:
    """The consumer. Everything above is setup for these."""

    def _run(self, svc, outcome_fields):
        svc.scrape_links = MagicMock(return_value=LINKS)
        svc.save_to_history = MagicMock(return_value=True)

        def _send(*_a, **kw):
            if outcome_fields is not None and kw.get("outcome") is not None:
                kw["outcome"].update(**outcome_fields)
            return True

        svc.send_to_jdownloader = MagicMock(side_effect=_send)
        svc.download_item("https://hdencode.org/x/", "A Film", None,
                          "2160p", "20 GB")
        assert svc.save_to_history.called, "the send path was never reached"
        return svc.save_to_history.call_args.kwargs["status"]

    def test_a_confirmed_api_delivery_is_archived_completed(self):
        status = self._run(_svc(), {"transport": "api", "confirmed": True})
        assert status == "completed"

    def test_an_unconfirmed_hand_off_is_NOT_archived_completed(self):
        status = self._run(_svc(),
                           {"transport": "clicknload", "confirmed": False})
        assert status == "delivered_unconfirmed", (
            "a hand-off nothing could confirm was written down as a confirmed "
            "delivery; no later query can separate the two")

    def test_the_folder_transport_is_also_unconfirmed(self):
        status = self._run(_svc(), {"transport": "folder", "confirmed": False})
        assert status == "delivered_unconfirmed"

    def test_a_transport_that_says_nothing_keeps_todays_behaviour(self):
        """Default-confirmed on an empty outcome, so a path not yet taught to
        report is not silently downgraded."""
        assert self._run(_svc(), None) == "completed"


class TestAnUnconfirmedRowStillSuppressesARegrab:
    """The other half of the requirement, and the easier one to get wrong.

    Marking it 'failed' would have been the lazy way to avoid claiming
    confirmation -- and it would cause the same release to be grabbed again,
    duplicating a download that already went out.

    Driven through save_to_history(), the real writer, against a real
    DatabaseManager -- so this exercises the actual column and the actual
    grabbed-set query, not a mock's idea of them.
    """

    def _write(self, db_manager, url, status):
        svc = _svc()
        svc.db = db_manager
        svc.save_to_history(url, "A Film", None, "2160p", "20 GB",
                            status=status)

    @pytest.mark.parametrize("status", ["completed", "delivered_unconfirmed"])
    def test_it_counts_as_grabbed(self, db_manager, status):
        url = "https://hdencode.org/%s/" % status
        self._write(db_manager, url, status)
        assert db_manager.is_downloaded(url) is True, (
            "a %r row did not suppress a re-grab" % status)

    def test_a_failed_row_does_NOT_count(self, db_manager):
        """Anti-vacuity: if every status counted, the test above would prove
        nothing about the one it names."""
        url = "https://hdencode.org/failed-one/"
        self._write(db_manager, url, "failed")
        assert db_manager.is_downloaded(url) is False


class TestTheFallbackNeverFiresOutsideServerMode:
    """A unit test must not be able to reach a real JDownloader.

    Before this gate, test_api_method_no_devices and test_api_method_exception
    -- which mock the cloud send to fail -- posted their dummy link to a REAL
    JDownloader on the developer's machine. They also became non-deterministic:
    pass or fail depending on whether JD happened to be running.
    """

    def test_a_non_server_mode_service_does_not_reach_out(self):
        svc = _svc()
        svc.server_mode = False
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("down")),              patch("backend.clicknload.probe") as probe,              patch("backend.clicknload.add_links") as add:
            assert svc.send_to_jdownloader(LINKS, "Pkg") is False
        assert not probe.called, "a desktop-mode service probed for a local JDownloader"
        assert not add.called, "a desktop-mode service handed links to a local JDownloader"

    def test_server_mode_still_falls_back(self):
        """The positive control. Gating must not disable the feature in the
        mode it exists for."""
        svc = _svc()
        svc.server_mode = True
        with patch.object(svc, "_connect_jd_device", side_effect=RuntimeError("down")),              patch("backend.clicknload.probe",
                   return_value=CnlResult(accepted=True, body="jdownloader=true;")),              patch("backend.clicknload.add_links",
                   return_value=CnlResult(accepted=True, http_status=200)):
            out = {}
            assert svc.send_to_jdownloader(LINKS, "Pkg", outcome=out) is True
        assert out["transport"] == "clicknload"
