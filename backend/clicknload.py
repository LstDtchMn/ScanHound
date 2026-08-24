"""JDownloader's Click'n'Load endpoint: a LOCAL hand-off that needs no cloud.

ScanHound reaches JDownloader through MyJDownloader's cloud API at
api.jdownloader.org. Two computers in the same house, routed through a server
on another continent. Over 27 hours of 2026-08-21/22 that produced eleven
outages -- thirteen 3-second read timeouts, five `[Errno 101] Network is
unreachable`, two dropped sessions.

JDownloader also listens locally on port 9666. Confirmed answering from inside
the container:

    GET /jdcheck.js  ->  jdownloader=true; var version='48637';

That path needs no cloud, no MyJD account, and no clipboard.

WHAT THIS CANNOT DO, and the reason every result here says so out loud:

    Click'n'Load is FIRE AND FORGET. A 200 means JDownloader accepted the
    POST. It does not mean a package was created, that the links parsed, or
    that anything will download. There is no response field that carries that,
    and no local endpoint to ask afterwards.

So a hand-off through here is DELIVERED but NOT CONFIRMED, and `confirmed` is
hardcoded False rather than left as a field somebody might one day set. The
whole point of the download archive model is that an item counts as grabbed
only when it truly reached JDownloader; a transport that cannot substantiate
that must not be able to claim it.

This module makes no policy decision about what to do with that. It reports.
"""
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional, Sequence

#: JDownloader's Click'n'Load listener. Bound to loopback on the JD host, so
#: from a container this is the host gateway rather than the LAN address --
#: measured 2026-08-22: host.docker.internal:9666 OPEN, 192.168.1.170:9666
#: refused.
DEFAULT_BASE_URL = "http://host.docker.internal:9666"

#: Generous next to myjdapi's 3s, because this is a request to a machine on the
#: same host rather than across the internet: if THIS is slow, something is
#: genuinely wrong and a longer wait costs nothing.
DEFAULT_TIMEOUT = 10


@dataclass(frozen=True)
class CnlResult:
    """What the endpoint said. Never more than that."""

    accepted: bool
    http_status: int = 0
    body: str = ""
    error: str = ""

    @property
    def confirmed(self) -> bool:
        """ALWAYS False, by construction, not by omission.

        Click'n'Load has no acknowledgement carrying package identity, so no
        amount of care here can turn acceptance into confirmation. A caller
        that wants confirmation has to get it from somewhere else -- the cloud
        API's package list, once it is reachable again.
        """
        return False

    def __bool__(self) -> bool:
        return self.accepted


def _get(url: str, timeout: int) -> CnlResult:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace").strip()
            return CnlResult(accepted=200 <= resp.status < 300,
                             http_status=int(resp.status or 0), body=body)
    except urllib.error.HTTPError as exc:
        return CnlResult(accepted=False, http_status=int(exc.code or 0),
                         error="HTTP %s" % exc.code)
    except Exception as exc:  # URLError, socket.timeout, DNS, anything
        return CnlResult(accepted=False,
                         error="%s: %s" % (type(exc).__name__, exc))


def probe(base_url: str = DEFAULT_BASE_URL,
          timeout: int = DEFAULT_TIMEOUT) -> CnlResult:
    """Is a JDownloader actually listening here?

    Checked against the body, not the status code: anything can answer 200.
    A proxy, a captive portal or an unrelated service on a recycled port would
    all pass a status-only check, and a hand-off to one of those would look
    exactly like a successful delivery.
    """
    res = _get("%s/jdcheck.js" % base_url.rstrip("/"), timeout)
    if not res.accepted:
        return res
    if "jdownloader=true" not in res.body.lower().replace(" ", ""):
        return CnlResult(
            accepted=False, http_status=res.http_status, body=res.body,
            error="something answered on this port, but it is not JDownloader")
    return res


def add_links(links: Sequence[str],
              package_name: str = "",
              destination: str = "",
              autostart: bool = True,
              base_url: str = DEFAULT_BASE_URL,
              timeout: int = DEFAULT_TIMEOUT,
              source: str = "ScanHound") -> CnlResult:
    """Hand links to a local JDownloader.

    Refuses an empty link list rather than sending an empty request that would
    answer 200 and deliver nothing -- which would read as a successful
    fallback in the log and leave the grab silently lost.
    """
    urls = [str(u).strip() for u in (links or []) if str(u or "").strip()]
    if not urls:
        return CnlResult(accepted=False,
                         error="no links to send; refusing an empty hand-off")

    params = {
        "urls": "\n".join(urls),
        "source": source,
        # JD's own naming for these, not ScanHound's.
        "autostart": "1" if autostart else "0",
    }
    if package_name:
        params["package"] = package_name[:50]
    if destination:
        params["dir"] = destination

    url = "%s/flash/add?%s" % (base_url.rstrip("/"), urllib.parse.urlencode(params))
    res = _get(url, timeout)
    if not res.accepted:
        return res
    # JD answers a short plain-text body. Treated as informational: the status
    # code is the only signal it actually commits to, and even that only means
    # "received".
    return CnlResult(accepted=True, http_status=res.http_status, body=res.body)
