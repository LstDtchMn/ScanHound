# rss-actions-dns-pinning (18-op) — reconciliation ledger

Action base = merge of RSS-completion head `a55b2e59` + PR J
(agent/public-error-boundary `52f26535`, SH-R09) = **`96582228`**
(branch `agent/hdencode-rss-actions`). Package expected_head placeholder →
`96582228`; 4 blob-placeholder guards (database.py, sources/hdencode.py,
client.ts, background_scanner.py) + 3 concrete blobs re-pointed to the real
action base (rss.py + hdencode_feed_client.py already matched; +page.svelte
re-pointed — differs from ChatGPT's synthetic because of my op[57] completion
reconciliation).

## PR J merge conflict (downloads.py) — resolved
Scrape endpoint `except`: HEAD (PR C/completion) had `logger.exception` + post-
except `diagnostic`/`record_scrape_outcome`; PR J replaced the except body with
`capture_public_exception`. Resolved as PR J's public-error except body + kept
the completion's `diagnostic = getattr(links,...)` / `record_scrape_outcome`
lines (the `diagnostic` var is read later). client.ts auto-merged (PR I RSS
methods + PR J error helpers both present). Merge validated: 17 passed
(test_public_error_boundary + test_scrape_outcomes).

## DNS-pinned transport review (handoff-required) — PASS
`_PinnedHTTPSConnection.connect()` opens the raw socket to the pinned IP but
wraps with `ssl.create_default_context()` (check_hostname=True, CERT_REQUIRED)
and `server_hostname=self.host` (approved hostname) → SNI + certificate
hostname verification enforced against hdencode.org, NOT weakened. Plus HTTPS-
only, host allowlist, private/loopback/link-local IP rejection (SSRF guard),
redirect re-validation, 2 MiB bounded gzip decompression, transport-auth gate.

## Defect A — op[9] anchor drift (database.py "persistent RSS action schema")
Same synthetic `# ── Versioned migrations` marker absent from the real tree.
Re-anchored the v6 hdencode_actions schema block to insert before the real
`# ── Stamp current version` marker (after the completion's v5 block). op[8]
"schema version 6" (SCHEMA_VERSION 5→6) matched fine.

## Defect B — package's OWN test at wrong layer (test_hdencode_action_database)
2 tests construct DatabaseManager directly and expect restart recovery on
reopen, but recovery is wired at the SERVICE layer
(HDEncodeActionService.__init__ → db.recover_hdencode_actions(), mirroring the
hydration-queue recovery pattern). `recover_hdencode_actions()` is CORRECT
(retrieving_links→queued 'recovered_after_restart'; submitting→needs_review
'submission_interrupted' + candidate action_state sync). Fixed the 2 tests to
call `reopened.recover_hdencode_actions()` (what the service does at startup) —
NOT moving recovery into DatabaseManager.__init__ (that would mutate on every
read-only open).

## Defect C — feed-client replacement drops validate_feed_url (sibling test)
op[11] replace_from replaces the ENTIRE backend/sources/hdencode_feed_client.py
with the DNS-pinned client, dropping the old module-level `validate_feed_url`
(folded into private `_validated_target`). Production imports only
`HDEncodeFeedClient` (rss_service.py:14) whose new `fetch(url,*,last_modified)`
matches the existing call — production SAFE. Only test_hdencode_rss_shadow.py
imported validate_feed_url (+ FeedResponse/_read_limited which survive) → its
collection ImportError aborted the whole broad. Fixed: import + call
validate_feed_url → `_validated_target` (same "Unsafe" loopback rejection).

## Verification
- Action README matrix: 14 passed (actions/action_database/feed_pinning/
  source_capabilities/rss_action_routes).
- test_hdencode_rss_shadow: 9 passed on the new client.
- Frontend: check 0 errors (353 files), vitest 373 passed (28 files), build clean.
- Full backend broad: (recorded on completion).
