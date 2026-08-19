# DV ingest key — security review response

**Repository:** `LstDtchMn/ScanHound`
**Branch:** `agent/dv-ingest-key`
**Reviewed head:** `358c1cf`
**Base:** `ad54e6a` (`main`)
**PR:** #60
**Date:** 2026-08-10

Verdict was REQUEST CHANGES on one MEDIUM blocker plus low-severity hardening. All four
findings are addressed. Full suite green (see the CI / whole-tree run).

## Finding 1 (MEDIUM, blocker) — redirect could forward the raw key — CLOSED

Confirmed real: `urllib` follows 301/302/303 by default and copies ordinary request headers
onto the redirected request, so a redirect from the endpoint could carry `X-DV-Ingest-Key` to
another origin. Fixed with both defenses you outlined, preferring the stronger one:

- `_post_rows` now attaches the key with `req.add_unredirected_header(...)`, which `urllib`
  never copies to a redirected request.
- A module-level `_INGEST_OPENER` (an `HTTPRedirectHandler` subclass whose `redirect_request`
  raises) **refuses any redirect** on the ingest POST. A 3xx on this fixed machine endpoint is
  configuration drift; it becomes an `HTTPError`, which `_post_rows` already treats as failure.

Discrimination test (`test_post_rows_refuses_redirect_and_never_leaks_key`): two real localhost
servers — the endpoint returns `302 Location: sink`; assert `_post_rows(...) is False` **and**
the sink is never contacted (the key never leaves the configured origin). Positive control
(`test_post_rows_direct_success_delivers_key`): a direct `200` delivers the key to the
configured host and succeeds — so the exfil test is not vacuously green. **Mutation-verified:**
reverting to the redirect-following opener fails the exfil test ("the redirect was followed").

## Finding 2 (LOW) — "raw secret never reaches the server" wording — CORRECTED

The secret necessarily travels in the request header. The docstring/comments now state the
true property: the raw secret is never **stored or configured** server-side — only its SHA-256
is — and it exists only transiently in the authenticated request and process memory. The
deploy note keeps loopback transport (`http://127.0.0.1:9721`) so it stays off the public
ingress path.

## Finding 3 (LOW) — path-canonicalization edges — PINNED

Added tests: with the ingest key, `POST /rename/dv-host-rows/` (trailing slash) and
`POST /rename//dv-host-rows` (double slash) both `401` — auth runs before any router
trailing-slash redirect, fail closed. `POST /RENAME/dv-host-rows` never reaches the handler
(`404`/`405`, case-sensitive routing). The exact `method + path` match is the boundary and is
now regression-pinned.

## Finding 4 (LOW) — malformed configured hash — VALIDATED + FAIL CLOSED

`dv_ingest_key_hash()` now requires a 64-char hex digest; a malformed value is treated as
**disabled** (returns `""`) with a one-time warning that does **not** log the value, so an
operator typo is diagnosable instead of a silent perpetual 401. Unit test covers non-hex,
too-short, and a valid uppercase digest normalizing to lowercase.

## Unchanged (your PASS verdicts)

The server-side scope (`_dv_ingest_authorized` gates on exact method+path; the middleware OR
cannot widen any other route), the constant-time hash compare, and the fail-closed
empty/missing cases are unchanged. Key generation is documented as high-entropy URL-safe
(`secrets.token_urlsafe(32)`).

## Ask

Please re-verify `agent/dv-ingest-key` at its new head: that the redirect is refused and the
key cannot reach a second origin (test + mutation), and that the four fixes hold. Merge/deploy
remain Jesse's.

---

## Round-2 re-review addendum — ambient proxy blocker — CLOSED

The re-review confirmed F1–F4 closed and found a NEW MEDIUM: `build_opener()` installs a
default `ProxyHandler` that honours `http_proxy` / system proxy settings, so the credentialed
POST could be routed through an ambient proxy that reads `X-DV-Ingest-Key`
(`add_unredirected_header` does not help — a proxy is the transport, not a redirect). Verified
against the code: the opener as built carried `proxies={'http': <env value>}`.

Fixed: `_INGEST_OPENER = build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())`.
`ProxyHandler({})` disables auto-discovered proxies, so the credentialed request now reaches
only the configured origin — no redirects, no ambient proxies.

Test (`test_post_rows_ignores_ambient_proxy`): a real proxy sink + a direct endpoint; with
`http_proxy` pointed at the sink and `no_proxy` cleared, `_post_rows` reaches the endpoint
directly, the sink is never contacted, and the key arrives at the endpoint. `http_proxy` is set
before the module loads so the mutation's default `ProxyHandler` picks it up.
**Mutation-verified:** removing `ProxyHandler({})` routes the request through the sink and fails
the test.

Also added the re-review's minor F4 suggestion: a `caplog` test proving the malformed-hash
warning fires exactly once and never logs the value.

Head now advances past `ddeac96`; full suite re-run to green.
