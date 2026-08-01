"""Regression cover for the 2026-07-31 external-report security review.

Two findings, both fixed in backend/api/main.py:

S1  Interactive API docs answered unauthenticated. Measured before the fix from
    a container on the shared `proxy` network: /openapi.json and /docs returned
    HTTP 200 while /results correctly returned 401. Now off unless
    SCANHOUND_ENABLE_API_DOCS is explicitly truthy.

S2  _within() compared lexically normalised strings, so a symlink inside the
    served root pointing outside it passed containment and the file was served.
    A 17-payload traversal corpus was run against the real function: 16 were
    correctly contained (absolute /etc/passwd, repeated ../, encoded forms,
    sibling-prefix) and the symlink was the sole escape. Now both sides are
    resolved with realpath before comparison.

Neither finding is known to have been exploited, and the symlink one required
write access inside the container to reach. They are fixed because the same
containment rule guards the file-move paths, where names originating from Plex,
JDownloader and scraped metadata do reach the filesystem.
"""

import os

import pytest
from fastapi.testclient import TestClient

from backend.api.main import _within, create_app


# ─────────────────────────── S1: API docs closed by default ────────────────

def _client(monkeypatch, docs_env=None):
    if docs_env is None:
        monkeypatch.delenv("SCANHOUND_ENABLE_API_DOCS", raising=False)
    else:
        monkeypatch.setenv("SCANHOUND_ENABLE_API_DOCS", docs_env)
    app = create_app(config_override={"plex_url": "", "plex_token": ""})
    return TestClient(app)


class TestApiDocsExposure:
    @pytest.mark.parametrize("path", ["/openapi.json", "/docs", "/redoc"])
    def test_docs_are_not_served_by_default(self, monkeypatch, path):
        """THE REGRESSION. Before the fix these returned 200 with the API
        schema to an unauthenticated caller.

        The property is that the SCHEMA IS NEVER EMITTED — not what status or
        content type carries the refusal. An earlier version of this test
        asserted `application/json` was absent, reasoning that the SPA catch-all
        would answer with index.html. That is a PROXY for the property and it
        depends on the environment: with no built frontend the catch-all is not
        mounted, the request 404s with `{"detail":"not found"}`, and the
        assertion trips even though nothing leaked. CI has no frontend build, so
        it failed there while passing locally.

        Worse, the real checks below were gated behind `status_code == 200`, so
        on the 404 path they never ran at all — the test could only fail for the
        wrong reason and could never pass for the right one.

        Assert the property directly, at any status."""
        with _client(monkeypatch) as c:
            r = c.get(path)
            body = r.text.lower()
            assert '"openapi"' not in body, f"{path} served the API schema"
            assert "swagger-ui" not in body, f"{path} served Swagger UI"
            assert "redoc" not in body or path != "/redoc", f"{path} served ReDoc"
            assert r.status_code != 200 or "text/html" in r.headers.get(
                "content-type", ""), (
                f"{path} returned 200 with {r.headers.get('content-type')!r}; "
                "only the SPA catch-all may answer these paths successfully")

    def test_docs_can_be_enabled_deliberately(self, monkeypatch):
        """The escape hatch must actually work, or someone will disable the
        guard wholesale to get their debugging back."""
        with _client(monkeypatch, "1") as c:
            r = c.get("/openapi.json")
            assert r.status_code == 200
            assert "openapi" in r.json()

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "maybe"])
    def test_only_explicit_truthy_values_enable_docs(self, monkeypatch, val):
        """Fail closed on anything ambiguous — an unset-but-present variable
        must not count as consent."""
        with _client(monkeypatch, val) as c:
            r = c.get("/openapi.json")
            body = r.text.lower()
            assert '"openapi"' not in body


# ─────────────────────────── S2: symlink containment ───────────────────────

class TestWithinContainment:
    def test_symlink_out_of_root_is_rejected(self, tmp_path):
        """THE REGRESSION. Before the fix this returned True: normpath is
        lexical and never touches the filesystem, so the link looked contained."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "SECRET.txt"
        secret.write_text("must never be served")

        link = root / "link.txt"
        try:
            os.symlink(secret, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted in this environment")

        assert _within(str(link), str(root)) is False

    def test_symlink_staying_inside_root_is_still_allowed(self, tmp_path):
        """Guard against over-correcting: a symlink that resolves back inside
        the root is legitimate and must keep working."""
        root = tmp_path / "root"
        (root / "sub").mkdir(parents=True)
        target = root / "sub" / "real.txt"
        target.write_text("fine")
        link = root / "alias.txt"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted in this environment")

        assert _within(str(link), str(root)) is True

    def test_root_reached_through_a_symlink_still_contains_its_children(self, tmp_path):
        """Resolving only the candidate would break any deployment whose root
        is itself behind a symlink — both sides must be resolved."""
        real_root = tmp_path / "real_root"
        real_root.mkdir()
        child = real_root / "asset.js"
        child.write_text("x")
        alias = tmp_path / "alias_root"
        try:
            os.symlink(real_root, alias)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted in this environment")

        assert _within(str(alias / "asset.js"), str(alias)) is True

    def test_sibling_prefix_still_rejected(self, tmp_path):
        """Pre-existing behaviour that must not regress: `.../root-evil` shares
        a string prefix with `.../root` but is not inside it."""
        root = tmp_path / "root"
        root.mkdir()
        sibling = tmp_path / "root-evil"
        sibling.mkdir()
        assert _within(str(sibling / "x.txt"), str(root)) is False

    def test_identical_path_is_contained(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        assert _within(str(root), str(root)) is True

    @pytest.mark.parametrize("payload", [
        "../SECRET.txt",
        "../../../../../../etc/passwd",
        "_app/../../SECRET.txt",
        "/etc/passwd",
    ])
    def test_traversal_payloads_remain_contained(self, tmp_path, payload):
        """These already passed before the fix. Asserted so a future change to
        the containment rule cannot quietly trade one class of escape for
        another."""
        root = tmp_path / "root"
        root.mkdir()
        candidate = os.path.normpath(os.path.join(str(root), payload))
        assert _within(candidate, str(root)) is False
