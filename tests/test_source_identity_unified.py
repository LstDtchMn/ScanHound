"""One registry, one classifier, and a subset rule so they cannot drift again.

WHY THIS EXISTS. Peer review round 5 found two classifiers answering "what source is
this?" independently -- `download_queue._source()` and
`download_service._source_page_kind()` -- and they had already diverged. Both
originally defaulted everything that was not DDLBase or Adit-HD to "hdencode"; round 4
fixed only the queue side; and their host lists differed too, the queue knowing about
Katfile, Turbobit, Hitfile, Fikper and Mega while the service's supported-host tuple
listed four.

Two registries answering one question is HOW that drift happened, so the tests here
pin the unification itself rather than only its current output.
"""
from backend import source_identity as si
from backend.download_queue import _source
from backend.download_service import (
    _SUPPORTED_DOWNLOAD_HOSTS,
    _source_page_kind,
)


class TestBothClassifiersAgree:
    """The whole point: one question, one answer."""

    CASES = [
        ("https://hdencode.org/a-release-2160p/", "hdencode"),
        ("https://www.hdencode.org/a/", "hdencode"),
        ("https://ddlbase.com/release/1", "ddlbase"),
        ("https://adit-hd.com/x", "adithd"),
        ("https://rapidgator.net/file/abc/x.rar", "direct_file"),
        ("https://1fichier.com/?abc", "direct_file"),
        ("https://some-new-host.example/x", "other"),
        ("", "other"),
    ]

    def test_the_service_classifier_matches_the_shared_one(self):
        for url, expected in self.CASES:
            assert _source_page_kind(url) == expected, url
            assert si.source_kind(url) == expected, url

    def test_the_queue_stores_the_same_identity(self):
        """The queue's stored strings differ only where the durable column already
        uses one -- `filehost` for `direct_file` -- because the active unique index
        is built on that value."""
        mapping = {"hdencode": "hdencode", "ddlbase": "ddlbase",
                   "adithd": "adithd", "direct_file": "filehost",
                   "other": "other"}
        for url, kind in self.CASES:
            assert _source(url) == mapping[kind], (url, kind)

    def test_neither_defaults_to_hdencode(self):
        """The defect itself. An unknown host must be NAMED, never assumed."""
        for url in ("https://unknown.example/x", "https://evil.test/a",
                    "not a url", ""):
            assert _source_page_kind(url) != "hdencode", url
            assert _source(url) != "hdencode", url

    def test_query_text_cannot_impersonate_a_source(self):
        """Path and query must never route: this was already guarded on the service
        side and must not be lost in the unification."""
        sneaky = "https://evil.example/?next=https://ddlbase.com"
        assert _source_page_kind(sneaky) == "other"
        assert _source(sneaky) == "other"


class TestTheRegistriesCannotDriftApart:
    """The structural guard, which is the part that actually prevents recurrence."""

    def test_supported_hosts_are_a_subset_of_the_identity_registry(self):
        """`_SUPPORTED_DOWNLOAD_HOSTS` answers a NARROWER question -- which direct
        hosts the downloader can hand off -- so it may be smaller. But a host the
        downloader supports that identity does not recognise would classify as
        "other" while still being downloaded, which is exactly the inconsistency
        review found. It must stay a subset."""
        missing = [h for h in _SUPPORTED_DOWNLOAD_HOSTS
                   if h not in si.DIRECT_FILE_HOSTS]
        assert not missing, (
            "these hosts are downloadable but unknown to the identity registry, so "
            f"they would classify as 'other': {missing}")

    def test_every_supported_host_classifies_as_direct_file(self):
        """The behavioural form of the same rule, in case the lists are ever
        restructured into something other than plain tuples."""
        for host in _SUPPORTED_DOWNLOAD_HOSTS:
            assert si.source_kind(f"https://{host}/file/1") == "direct_file", host

    def test_the_queue_holds_no_host_literals_of_its_own(self):
        """A second literal list of file hosts is how the drift started.

        The queue must hold none: it asks only "what identity is this?", which the
        shared module answers. download_service legitimately still lists hosts, but
        for the DIFFERENT question of what it can hand off -- and the subset test
        above keeps that list honest.
        """
        import inspect
        from backend import download_queue
        src = inspect.getsource(download_queue)
        for host in ("rapidgator.net", "1fichier.com", "nitroflare.com",
                     "katfile.com", "mega.nz"):
            assert host not in src, (
                f"download_queue contains the host literal {host!r}; identity hosts "
                "belong in backend.source_identity, and a second copy is how the two "
                "registries drifted apart in the first place")

    def test_the_configured_host_wins_over_the_default(self):
        """Identity follows configuration, so a mirror classifies correctly and the
        old default domain stops being authoritative."""
        assert si.source_kind("https://hdencode.example.net/a/",
                              "https://hdencode.example.net") == "hdencode"
        assert si.source_kind("https://hdencode.org/a/",
                              "https://hdencode.example.net") == "other"
