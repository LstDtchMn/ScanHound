"""The local Click'n'Load hand-off: what it delivers, and what it must not claim.

ScanHound talks to JDownloader only through MyJDownloader's cloud. Eleven
outages in 27 hours (2026-08-21/22) came from that path. JDownloader also
listens locally on 9666, which needs no cloud and no account.

The danger in adding a second delivery path is not that it fails -- a failure
is visible. It is that it SUCCEEDS WEAKLY: Click'n'Load answers 200 for
"received", never for "package created", so a hand-off through it is evidence
of less than an API send is. The archive model counts an item as grabbed only
when it truly reached JDownloader, so these tests exist mostly to pin down that
this transport can never claim confirmation.

Nothing here touches a real JDownloader. The server under test is a local stub.
"""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from backend.clicknload import CnlResult, add_links, probe

JD_BODY = b"jdownloader=true;\nvar version='48637';\n"


class _Handler(BaseHTTPRequestHandler):
    """Records what it was asked for; answers whatever the test configured."""

    def do_GET(self):  # noqa: N802  (BaseHTTPRequestHandler's contract)
        parsed = urlparse(self.path)
        self.server.requests.append((parsed.path, parse_qs(parsed.query)))
        status, body = self.server.reply.get(
            parsed.path, self.server.reply.get("*", (404, b"nope")))
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass  # keep pytest output readable


@pytest.fixture
def jd():
    """A stub listening on a real socket, so the transport does real HTTP."""
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.requests = []
    srv.reply = {"/jdcheck.js": (200, JD_BODY), "/flash/add": (200, b"success")}
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.base = "http://127.0.0.1:%d" % srv.server_address[1]
    yield srv
    srv.shutdown()
    srv.server_close()


class TestProbeIdentifiesJDownloaderNotJustAnything:

    def test_a_real_jdownloader_body_is_accepted(self, jd):
        res = probe(base_url=jd.base)
        assert res.accepted, res.error
        assert res.http_status == 200

    def test_a_200_from_something_else_is_REFUSED(self, jd):
        """The control that matters. Anything can answer 200 on a recycled
        port -- a proxy, a captive portal, an unrelated service. A status-only
        check would hand a grab to it and log a successful delivery."""
        jd.reply["/jdcheck.js"] = (200, b"<html>hello from nginx</html>")
        res = probe(base_url=jd.base)
        assert not res.accepted
        assert "not JDownloader" in res.error

    def test_an_error_status_is_refused(self, jd):
        jd.reply["/jdcheck.js"] = (503, b"busy")
        res = probe(base_url=jd.base)
        assert not res.accepted
        assert res.http_status == 503

    def test_nothing_listening_is_refused_without_raising(self):
        res = probe(base_url="http://127.0.0.1:1", timeout=2)
        assert not res.accepted
        assert res.error

    def test_whitespace_variants_of_the_marker_still_match(self, jd):
        """Anti-vacuity: the check must not be so strict it rejects a real JD
        that formats its response slightly differently."""
        jd.reply["/jdcheck.js"] = (200, b"jdownloader = true ;\nvar version='1';")
        assert probe(base_url=jd.base).accepted


class TestAddLinksSendsWhatJDownloaderExpects:

    LINKS = ["https://rapidgator.net/file/aaa", "https://rapidgator.net/file/bbb"]

    def test_the_links_arrive_newline_separated(self, jd):
        assert add_links(self.LINKS, base_url=jd.base).accepted
        path, q = jd.requests[-1]
        assert path == "/flash/add"
        assert q["urls"][0].split("\n") == self.LINKS

    def test_package_destination_and_autostart_are_passed(self, jd):
        add_links(self.LINKS, package_name="Some Film (2026) [4K]",
                  destination="G:\\Downloads", base_url=jd.base)
        _, q = jd.requests[-1]
        assert q["package"][0] == "Some Film (2026) [4K]"
        assert q["dir"][0] == "G:\\Downloads"
        assert q["autostart"][0] == "1"

    def test_autostart_can_be_turned_off(self, jd):
        add_links(self.LINKS, autostart=False, base_url=jd.base)
        assert jd.requests[-1][1]["autostart"][0] == "0"

    def test_the_package_name_is_truncated_like_the_api_path(self, jd):
        add_links(self.LINKS, package_name="x" * 120, base_url=jd.base)
        assert len(jd.requests[-1][1]["package"][0]) == 50

    def test_an_empty_link_list_sends_NOTHING(self, jd):
        """An empty POST would answer 200 and deliver nothing -- a successful
        fallback in the log, and a grab silently lost."""
        res = add_links([], base_url=jd.base)
        assert not res.accepted
        assert "empty hand-off" in res.error
        assert jd.requests == [], "it contacted JDownloader anyway"

    def test_blank_entries_are_stripped_but_real_ones_survive(self, jd):
        assert add_links(["", "  ", self.LINKS[0]], base_url=jd.base).accepted
        assert jd.requests[-1][1]["urls"][0] == self.LINKS[0]

    def test_a_list_of_only_blanks_counts_as_empty(self, jd):
        assert not add_links(["", "   ", None], base_url=jd.base).accepted
        assert jd.requests == []

    def test_a_server_error_is_reported_not_swallowed(self, jd):
        jd.reply["/flash/add"] = (500, b"boom")
        res = add_links(self.LINKS, base_url=jd.base)
        assert not res.accepted
        assert res.http_status == 500


class TestThisTransportCanNeverClaimConfirmation:
    """The whole reason this file exists.

    An API send is confirmed by the device accepting the payload. Click'n'Load
    has no acknowledgement carrying package identity, so acceptance is strictly
    weaker evidence -- and the archive model rests on that distinction.
    """

    def test_a_successful_hand_off_is_still_unconfirmed(self, jd):
        res = add_links(["https://x.test/a"], base_url=jd.base)
        assert res.accepted
        assert res.confirmed is False, (
            "an accepted Click'n'Load POST reported itself as confirmed; "
            "nothing in the response can substantiate that")

    def test_confirmed_is_false_on_a_failure_too(self, jd):
        jd.reply["/flash/add"] = (500, b"boom")
        assert add_links(["https://x.test/a"], base_url=jd.base).confirmed is False

    def test_confirmed_cannot_be_set_by_a_caller(self):
        """It is a property, not a field. A future caller cannot flip it, and a
        future field named `confirmed` cannot quietly shadow it."""
        res = CnlResult(accepted=True, http_status=200)
        with pytest.raises(AttributeError):
            res.confirmed = True

    def test_the_result_is_frozen(self):
        res = CnlResult(accepted=True, http_status=200)
        with pytest.raises(Exception):
            res.accepted = False

    def test_truthiness_tracks_acceptance_only(self, jd):
        assert bool(add_links(["https://x.test/a"], base_url=jd.base)) is True
        assert bool(add_links([], base_url=jd.base)) is False
