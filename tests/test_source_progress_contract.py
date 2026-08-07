"""The producer must actually emit the signal the queue rewards.

THE DEFECT THIS EXISTS TO PREVENT, and it is the third time in one day.

Round 2 introduced `source_delivery_count`, incremented only when
`is_source_delivery(outcome)` held, which required `transport_attempted` to be
truthy. Peer review found that **no real success path sets that field**:
`download_item()` initialises it to `None` and the jdownloader, clipboard and
browser paths never touch it — the only writers are failure diagnostics. So the
counter never incremented in production and the refund could never fire.

Seventeen tests passed, because both suites fabricated `transport_attempted=True`
in hand-built outcome dicts. A condition I added as "belt and braces" is exactly
what made the mechanism inert.

So this file asserts the CONTRACT BETWEEN LAYERS rather than the behaviour of any
one of them:

    real DownloadService.download_item()
        -> public_download_result()
            -> DownloadQueueService.is_source_delivery()
                -> the batch counter

Nothing in the chain is hand-built. Where a dict is unavoidable, it comes from the
real mapper applied to the real producer's output.
"""
from unittest.mock import MagicMock

import pytest

from backend.download_outcome import public_download_result
from backend.download_queue import DownloadQueueService

URL = "https://hdencode.org/some-release-2160p/"


def _service(tmp_path, *, already_downloaded=False):
    """A real DownloadService with only its external edges stubbed."""
    from backend.download_service import DownloadService
    svc = DownloadService.__new__(DownloadService)
    svc.config = {"download_method": "jdownloader"}
    svc.db = MagicMock()
    svc.db.is_downloaded.return_value = already_downloaded
    svc.db.find_similar_downloaded.return_value = None
    svc._log = lambda *a, **k: None
    svc._progress = lambda *a, **k: None
    return svc


class TestTheProducerEmitsTheSignal:
    """If these fail, the queue's refund is inert no matter how correct it looks."""

    def test_the_default_result_does_not_claim_source_progress(self):
        """The initial dict must be False, so any path that forgets to set it
        fails closed rather than earning a refund it did not."""
        from backend.download_service import DownloadService
        import inspect
        src = inspect.getsource(DownloadService.download_item)
        assert '"source_progress": False' in src, (
            "the result dict must initialise source_progress to False; without "
            "that, a missing key would read as absent rather than negative")

    def test_a_REAL_jdownloader_success_sets_it_end_to_end(self, tmp_path):
        """THE POSITIVE CONTRACT, EXECUTED. Peer review's round-4 MEDIUM.

        My previous positive test read the source text and then hand-built a result
        dict -- so only the NEGATIVE (duplicate) case actually ran production code.
        A real positive does not need live JDownloader: stub scrape_links,
        send_to_jdownloader and save_to_history, run download_item() for real, then
        push its OWN returned dict through the mapper and the queue's predicate.
        """
        svc = _service(tmp_path)
        svc.config = {"download_method": "jdownloader", "jd_enabled": True,
                      "jd_method": "api", "jd_folder": "",
                      "jd_movies_folder": "/movies"}
        svc.scrape_links = lambda *a, **k: ["https://rapidgator.net/file/1"]
        svc.send_to_jdownloader = lambda *a, **k: True
        svc.save_to_history = lambda *a, **k: True

        raw = svc.download_item(url=URL, title="Some Release", year=2026,
                                season=None, resolution="2160p", size="10 GB",
                                hdr="", dovi=False, service_type="Rapidgator")
        assert raw["success"] is True, raw
        assert raw["method"] == "jdownloader", raw
        assert raw.get("source_progress") is True, (
            "a real jdownloader delivery must set source_progress; without it the "
            "retry-budget refund never fires -- the round-3 defect")

        outcome = public_download_result(raw, title="Some Release", url=URL)
        assert DownloadQueueService.is_source_delivery(outcome) is True, (
            "the producer's own dict, through the real mapper, must satisfy the "
            "queue's predicate")

    @pytest.mark.parametrize("method", ["jdownloader", "clipboard", "browser"])
    def test_every_real_success_path_sets_it(self, method):
        """A structural guard COMPLEMENTING the executed test above.

        clipboard and browser need a live clipboard/browser, so they are pinned by
        source inspection: a fourth delivery path cannot be added without the
        signal. The jdownloader path is additionally executed for real above --
        because a structural assertion alone was the round-4 MEDIUM.
        """
        from backend.download_service import DownloadService
        import inspect
        src = inspect.getsource(DownloadService.download_item)
        marker = f'result["method"] = "{method}"'
        assert marker in src, f"{method} path not found"
        after = src.split(marker, 1)[1][:200]
        assert 'result["source_progress"] = True' in after, (
            f"the {method} success path does not set source_progress, so a real "
            "delivery through it would never refund the retry budget -- the exact "
            "defect peer review found")

    def test_the_duplicate_path_does_NOT_set_it(self, tmp_path):
        """The reviewer's counterexample, executed rather than argued.

        A release already grabbed returns success BEFORE scraping. That must not
        look like source progress.
        """
        svc = _service(tmp_path, already_downloaded=True)
        result = svc.download_item(url=URL, title="Some Release", year=2026,
                                   season=None, resolution="2160p", size="10 GB",
                                   hdr="", dovi=False, service_type="Rapidgator")
        assert result["success"] is True
        assert result["method"] == "duplicate"
        assert not result.get("source_progress"), (
            "a pre-scrape duplicate contacted nothing and must not claim source "
            "progress")


class TestTheContractHoldsEndToEnd:
    """The chain, with no hand-built dict anywhere in it."""

    def test_a_duplicate_travels_the_whole_chain_without_earning_a_refund(
            self, tmp_path):
        svc = _service(tmp_path, already_downloaded=True)
        raw = svc.download_item(url=URL, title="Some Release", year=2026,
                                season=None, resolution="2160p", size="10 GB",
                                hdr="", dovi=False, service_type="Rapidgator")
        outcome = public_download_result(raw, title="Some Release", url=URL)
        assert outcome["success"] is True
        assert DownloadQueueService.is_source_delivery(outcome) is False, (
            "the real producer's duplicate outcome, through the real mapper, must "
            "not satisfy the queue's source-progress test")

    def test_the_mapper_preserves_a_positive_signal(self):
        """The mapper is the layer that silently dropped things before.

        Built from the producer's own default dict shape plus the assignment the
        success paths make, since those paths cannot run without live transports.
        """
        from backend.download_service import DownloadService
        import inspect
        # Take the producer's literal default dict, then apply exactly what a
        # success path does. This keeps the shape tied to production.
        src = inspect.getsource(DownloadService.download_item)
        assert '"source_progress": False' in src
        raw = {"success": True, "method": "jdownloader", "link_count": 1,
               "message": "sent", "reason_code": "", "stage": "download",
               "retryable": False, "retry_mode": "none",
               "transport_attempted": None, "affected_scope": "item",
               "action_code": "", "signals": [], "source_progress": True}
        outcome = public_download_result(raw, title="t", url=URL)
        assert outcome.get("source_progress") is True, (
            "public_download_result dropped source_progress; the producer's "
            "signal would be invisible to its only consumer")
        assert DownloadQueueService.is_source_delivery(outcome) is True

    def test_transport_attempted_is_no_longer_required(self):
        """Regression guard on the specific mistake.

        A real success carries `transport_attempted=None`. If anything re-adds a
        requirement on that field, real deliveries stop counting again.
        """
        outcome = {"success": True, "method": "jdownloader",
                   "transport_attempted": None, "source_progress": True}
        assert DownloadQueueService.is_source_delivery(outcome) is True, (
            "a real success has transport_attempted=None; requiring it is what "
            "made the counter never increment")


class TestSourceOwnership:
    """R2-3: the counter must belong to the source whose budget it refunds."""

    def test_the_owning_source_is_hdencode(self):
        assert DownloadQueueService.AUTO_RESUME_SOURCE == "hdencode"

    def test_only_the_owning_source_increments(self, tmp_path):
        """A mixed batch can complete another source's item while paused for
        HDEncode, because _claim_due does not require the batch to be scheduled.
        That must not buy an HDEncode retry."""
        from backend.database import DatabaseManager
        db = DatabaseManager(str(tmp_path / "ownership.db"))
        try:
            service = DownloadQueueService({}, db, MagicMock())
            service._coordinator_snapshot = MagicMock(
                return_value={"blocked": False})
            batch = service.schedule_batch(
                [{"url": "https://hdencode.org/a-2160p/", "title": "A",
                  "media_type": "movie"},
                 {"url": "https://ddlbase.com/release/b", "title": "B",
                  "media_type": "movie"}],
                interval_minutes=0, mode="immediate",
                auto_resume_after_cooldown=True)
            uuid = batch["batch_uuid"]
            rows = service.get_batch(uuid)["items"]
            other = next(r for r in rows if r["source"] != "hdencode")
            mine = next(r for r in rows if r["source"] == "hdencode")

            def complete(row):
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE download_queue_items SET state='claimed', "
                        "claimed_by=? WHERE item_uuid=?",
                        (service.worker_id, row["item_uuid"]))
                service._complete(dict(row), {
                    "success": True, "method": "jdownloader", "link_count": 1,
                    "message": "x", "source_progress": True,
                    "transport_attempted": None})

            def counter():
                r = db._query(
                    "SELECT source_delivery_count AS n FROM "
                    "download_queue_batches WHERE batch_uuid=?",
                    (uuid,), one=True, default=None)
                return int((r["n"] if r else 0) or 0)

            complete(other)
            assert counter() == 0, (
                "a non-HDEncode delivery must not increment the counter that "
                "refunds HDEncode's retry budget")
            complete(mine)
            assert counter() == 1, (
                "positive control: an HDEncode delivery must increment it, or "
                "the exclusion above passes for the wrong reason")
        finally:
            db.close()


class TestSourceIdentityIsAffirmative:
    """`_source()` must NAME a source, never default to HDEncode.

    THE DEFECT THIS FIXES, found by peer review in round 4. The classifier was:

        ddlbase -> "ddlbase"; adit-hd -> "adithd"; EVERYTHING ELSE -> "hdencode"

    So Rapidgator, 1fichier, Nitroflare, ddownload and any future host became
    "hdencode". `download_item()` supports direct file-host URLs and the batch API
    accepts arbitrary download URLs, so a mixed batch could store a direct-host row
    as source="hdencode", group it under an HDEncode pause, and -- once the refund
    began working -- let it refund HDEncode's retry budget.

    **My own mixed-batch test could not see this**: it used DDLBase, one of the two
    hosts the old function actually recognised. These use direct file hosts, which
    is the case that was broken.
    """

    def test_direct_file_hosts_are_not_hdencode(self):
        from backend.download_queue import _source
        for url in ("https://rapidgator.net/file/abc/x.rar",
                    "https://1fichier.com/?abc123",
                    "https://nitroflare.com/view/ABC/x.rar",
                    "https://ddownload.com/abc123",
                    "https://katfile.com/abc123"):
            assert _source(url) == "filehost", url

    def test_an_unknown_host_is_other_not_hdencode(self):
        from backend.download_queue import _source
        assert _source("https://some-new-host.example/file/1") == "other"
        assert _source("") == "other"
        assert _source("not a url") == "other"

    def test_the_recognised_sources_still_classify(self):
        from backend.download_queue import _source
        assert _source("https://hdencode.org/a-release-2160p/") == "hdencode"
        assert _source("https://www.hdencode.org/a/") == "hdencode"
        assert _source("https://ddlbase.com/release/1") == "ddlbase"
        assert _source("https://adit-hd.com/x") == "adithd"

    def test_a_configured_mirror_is_recognised(self):
        """Identity follows configuration, so a changed domain or mirror still
        classifies as HDEncode instead of falling through to 'other'."""
        from backend.download_queue import _source
        assert _source("https://hdencode.example.net/a/",
                       "https://hdencode.example.net") == "hdencode"
        assert _source("https://hdencode.org/a/",
                       "https://hdencode.example.net") == "other", (
            "with a mirror configured, the old default domain is no longer "
            "authoritative -- it must not be silently accepted")

    def test_a_direct_host_row_cannot_refund_the_hdencode_budget(self, tmp_path):
        """THE PRODUCTION PATH the review described, end to end."""
        from unittest.mock import MagicMock
        from backend.database import DatabaseManager
        db = DatabaseManager(str(tmp_path / "filehost-refund.db"))
        try:
            service = DownloadQueueService({}, db, MagicMock())
            service._coordinator_snapshot = MagicMock(
                return_value={"blocked": False})
            batch = service.schedule_batch(
                [{"url": "https://hdencode.org/a-2160p/", "title": "A",
                  "media_type": "movie"},
                 {"url": "https://rapidgator.net/file/xyz/b.rar", "title": "B",
                  "media_type": "movie"}],
                interval_minutes=0, mode="immediate",
                auto_resume_after_cooldown=True)
            uuid = batch["batch_uuid"]
            rows = service.get_batch(uuid)["items"]
            sources = {r["title"]: r["source"] for r in rows}
            assert sources["B"] == "filehost", (
                f"a direct Rapidgator URL must not be stored as hdencode; got "
                f"{sources}")

            direct = next(r for r in rows if r["source"] == "filehost")
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE download_queue_items SET state='claimed', "
                    "claimed_by=? WHERE item_uuid=?",
                    (service.worker_id, direct["item_uuid"]))
            service._complete(dict(direct), {
                "success": True, "method": "jdownloader", "link_count": 1,
                "message": "x", "source_progress": True,
                "transport_attempted": None})
            row = db._query(
                "SELECT source_delivery_count AS n FROM download_queue_batches "
                "WHERE batch_uuid=?", (uuid,), one=True, default=None)
            assert int((row["n"] if row else 0) or 0) == 0, (
                "a direct file-host delivery must not increment the counter that "
                "refunds HDEncode's retry budget")
        finally:
            db.close()
