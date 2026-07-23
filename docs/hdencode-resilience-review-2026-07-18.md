# Review: HDEncode Access-Resilience Plan

**Reviewer:** Claude (Opus 4.8)
**Date:** 2026-07-18
**Reviewing:** `scanhound_hdencode_resilience_plan_for_claude.md` (items 1–7, 9)
**Method:** every assumption below was checked against the code at `X:\Docker Apps\ScanHound`, not inferred from the plan text.

---

## 0. Scope boundary

I'll work on everything whose effect is *less* traffic to HDEncode, *faster* stopping when blocked, honest error reporting, and asking the operator what they want. That is the large majority of this document, and it is a genuinely good plan.

I won't work on making ScanHound's automation harder for HDEncode to detect, or on using a human to clear an interactive challenge so that automation can resume against a gate built to stop automation. That rules out **Item 6 (noVNC handoff)** as specified, and it bounds **Item 7** to legitimate session continuity rather than fingerprint evasion. Details in §3.

This is the same line I drew earlier on rewriting the link extractor against their page structure, and it is unchanged. Note that it costs this plan very little: Item 6 is also the item with the weakest technical premise (§3, Item 6) and the largest new attack surface.

---

## A. Findings — which assumptions hold

### A.1 CONFIRMED, and worse than the plan assumes: detail-before-relevance

§4.2's suspicion is correct. `all_posts` is *every* non-cached listing URL, and all of it is fetched before any relevance test:

- `scanner_service.py:463` `await self._process_posts(all_posts, ...)`
- `scanner_service.py:479` `await self._match_against_plex(scan_type)`
- `scanner_service.py:485` `await self._enrich_metadata_async()`

The only filter between "found a URL" and "fetched its detail page" is the URL cache (`scanner_service.py:676-681`). Plex state, download history, rating/genre filters and auto-grab criteria are all evaluated on objects that *already required a detail fetch*.

**What the plan misses:** the concurrency. `ThreadPoolExecutor(max_workers=num_threads)` at `scanner_service.py:766` submits every URL at once, with `scan_threads` defaulting to **10** (`config.py:523`, clamp 1–50 at `config.py:603`). There is **zero pacing** on the detail path — no sleep, no jitter, no token bucket. The only `time.sleep` in `detail_scraper.py` (`:71,74,78`) fires on failure. Listing pages get `asyncio.sleep(0.3)` (`scanner_service.py:694`); detail pages get nothing.

So a first background scan or cache rebuild issues detail requests from 10 threads as fast as the site responds. This is almost certainly the dominant cause of whatever triggered the blocking, and it is fixable in an afternoon — see Phase 0.

### A.2 A correct, centralized, 429-aware HTTP client already exists — as dead code

`backend/network.py::AsyncRequestManager` implements exactly what Item 2 §5.8 proposes: 3 attempts, 429 → `2 ** attempt` exponential backoff (`:170-176`), 4xx-except-429 → immediate `None` (`:179-182`). It is imported by **`tests/test_network.py` only** and referenced in `DEVELOPMENT.md:331`. No production module uses it.

Item 2 should start by reviving and adapting this, not greenfielding a broker.

### A.3 The block classifier already exists and is rich — its return value is thrown away

This is the single highest-value finding in the review. `download_service._log_page_diagnostics` (`:1161-1244`) already distinguishes nearly every class §2.4 asks for:

| §2.4 class | Already detected at |
|---|---|
| Browser/network failure | `:1181-1189` (`err_`, `dns_probe`, `connection was reset`) |
| Cloudflare JS interstitial | `:1190-1198`, `:1234-1240` (`just a moment`, `cf-chl`, `checking your browser`) |
| Interactive CAPTCHA/Turnstile | `:1227-1232` (`turnstile`, `challenges.cloudflare`, `recaptcha`, `hcaptcha`) |
| Changed page layout | `:1208-1226` (enumerates available controls/forms) |

Its signature is `-> None` (`:1161`). Every caller discards it and returns bare `[]` (`:1371, :1390, :1406, :1468`). `download_item` then collapses everything to a two-way string (`:1988-2006`) in which a Cloudflare wall and a host-not-present both produce the *same* message, because a wall returns `[]` without raising and `scrape_failed` is only set when `scrape_links` raises (`:1982-1984`). The DB row is written `status="failed"` with no reason code (`downloads.py:211`).

**Item 3 is therefore mostly a plumbing job, not a modelling job.** Change the return type, thread a reason code through `scrape_links` → `download_item` → DB → API → UI. That alone delivers §6.10's acceptance criteria "a CAPTCHA and a network failure produce different user messages" and "a layout change is not automatically classified as CAPTCHA."

The scan path has no classification at all: `_crawl_pages` lumps 403/429/503 into one undifferentiated "block" (`scanner_service.py:647`), and a `None` from `scrape_details` is silently dropped (`:756-757`), so a blocked detail page and an unparseable one are indistinguishable.

### A.4 INCORRECT PREMISE: Item 7 has no profile to preserve

§10.1 says "maintaining a stable Chromium profile … across link-grab operations and controlled restarts," and §10.2 asks where the profile is stored. The answer is that **no `user_data_dir` is ever set** — a repo-wide grep returns zero hits in `backend/`. Options built at `download_service.py:1009-1016` are window-size, GPU, sandbox, shm, start-minimized, binary_location. Nothing else.

Consequently `undetected_chromedriver` 3.5.5 assigns its own: `user_data_dir = os.path.normpath(tempfile.mkdtemp())` with `keep_user_data_dir = False`, and `rmtree`s the entire profile on quit. Every recycle destroys all cookies and clearance; every new driver starts as a brand-new client.

`docker/entrypoint.sh:35-37` clears `/data/.config/chromium/Singleton*` lock files — but that tree is Chromium default-profile scaffolding, not the browsing profile in use (its `Cookies` DB is an empty 16 KB schema). No application code deletes a whole profile; uc's own `rmtree` does it on every normal recycle.

**Implications:**
1. Item 7 is net-new work ("create persistence"), not hardening ("protect existing persistence"). Re-estimate accordingly.
2. §10.10's acceptance criterion "browser cookies survive a normal container restart" currently fails by construction.
3. It removes Item 6's core justification — see §3.

### A.5 There is no working off switch for HDEncode

`ddlbase_enabled` (`config.py:94`) and `adithd_enabled` (`config.py:99`) exist. **`hdencode_enabled` does not.** `PUT /sources/hdencode {"enabled": false}` (`sources.py:26-28`) writes a key no validator knows, and the live scan path (`_build_sources`, `scanner_service.py:499-561`) never consults source-enabled state at all — it branches on hardcoded `source_type` strings.

This matters directly to Item 9. §11.5 "Negative response → disable or limit HDEncode automation according to their request" describes a capability the application does not have. **Do not send the outreach in Item 9 until you can honour a "no."**

### A.6 Scheduled scans don't run

`AppService._start_scheduler` (`app_service.py:715`) calls `self._scan_trigger()` at `:752`, but `set_scan_trigger` (`:711`) is never called outside tests; the thread logs "Scheduled scan interval reached (no trigger registered)" (`:757-760`). The only working periodic scan is `BackgroundScanner`.

So `SCHEDULED_SCAN` in §5.4's request-class list models traffic that doesn't exist. Either wire the trigger or drop the class — but know which you're doing.

### A.7 Two parallel source systems, and the policy metadata is wired to the wrong one

- **System A** — hardcoded `if/elif` in `scanner_service._build_sources` (`:499-561`). *This is what issues scan traffic.*
- **System B** — `SourceRegistry` plugin system (`sources/registry.py:22`) with a real `SourceConfig` dataclass (`sources/base.py:32-45`) carrying `rate_limit`, `timeout`, `priority`, `requires_auth`.

HDEncode declares `rate_limit=2.0` (`hdencode.py:51`), honoured only in `SourceBase._fetch_html` (`base.py:215-223`) — which serves only the pipeline `search` path. `scanner_service.py` never imports `SourceConfig`; its fetches use hardcoded 15 s/20 s timeouts and no rate limit. Worse, `api/routes/pipeline.py:108` builds a **new `SourceRegistry()` per request**, so even that spacing state is discarded between calls.

A per-source policy layer is half-built and attached to the system that generates the least traffic. The broker's real job is to make System A honour System B's declared policy.

### A.8 Reusable infrastructure (don't rebuild)

- **WebSocket hub** — `api/ws.py`, with a thread-safe `broadcast_sync()` (`:68`) already used by scanner/downloads/renames. Source-health push is a new `type` value, nothing more.
- **DB migrations** — adding a `request_events` table is genuinely cheap: one idempotent `CREATE TABLE IF NOT EXISTS` in `init_db` (`database.py:225`), optional indexes at `:569-588`, **no** `SCHEMA_VERSION` bump.
- **Notifications** — 6 channels in `notifications.py`. **No Gotify channel**, which is worth noting since Gotify is this deployment's notification hub; a `GenericWebhookChannel` (`:320`) can target it, or a Gotify subclass is ~30 lines.
- **Incident de-dup does not exist.** The only suppression is 5-second batching by `NotificationType` (`notifications.py:665-690`); `_history` is in-memory, capped at 100, lost on restart. §6.6's "notify once per incident" is net-new.
- **Per-source health tracking is thinner than remembered** — `background_scanner.py:206` keeps `{source, new, error}` for the *most recent run only*, in memory, no DB table, wiped on restart. No consecutive-failure count, no last-success timestamp.

### A.9 Config cost is higher than it looks

A new key requires **4 mandatory** edits (`config.py` TypedDict, `_DEFAULT_CONFIG`, `SettingsUpdate` Pydantic model — mandatory because `extra="forbid"` at `settings.py:61` rejects unknown keys with 422, and `frontend/types.ts`), **5** with a UI control, **6** with validation or masking.

§13 proposes **17 keys** ⇒ roughly 70–100 edit sites. Your own instinct in §13 to collapse to a mode enum is correct; treat it as a requirement, not an option. Note also `config.py:88-91` documents that `source_2160p`/`source_remux`/`source_tv_packs` were *deleted* precisely because they were write-only settings no scan path read — the exact failure mode 17 new keys invites.

### A.10 Dead code to delete rather than route through a broker

§5.2 asks for a complete call-site inventory. Four of the paths you'd otherwise wire up have no production callers:

- `sources/hdencode.py:117` `fetch_page`, `:366` `fetch_release_details`
- `sources/hdencode.py:456-461` `fetch_download_links` — builds its **own plain `webdriver.Chrome`**, bypassing the cached driver entirely (its own comment at `:446-448` admits this)
- `link_scraper.py:56` `scrape_links_with_driver`
- `network.py::AsyncRequestManager` (see A.2 — revive rather than delete)

Deleting the first three shrinks the broker's surface before it's written.

---

## B. Architecture recommendation

**Do not build a general "source access layer" abstraction first.** The live traffic comes from two concrete places — `scanner_service._crawl_pages`/`_process_posts` (cloudscraper, 10 threads) and `download_service.scrape_links` (Selenium, serialized by `_driver_lock` at `:1296`). Make those two honour a shared policy object; generalize later if a third source needs it.

- **Policy source of truth:** extend the existing `SourceConfig` (`sources/base.py:32-45`) with `min_request_interval`, `max_concurrency`, `cooldown_seconds`. It already has `rate_limit`/`timeout`/`priority` and is already per-source.
- **Broker shape:** a synchronous gate (`acquire(op, priority) -> token`) wrapping the existing thread pool, not an async rewrite. `scrape_links` is already serialized by an RLock; the scan path is `run_in_executor` over threads. An async broker would force a much larger refactor for no traffic benefit.
- **Selenium in the broker:** account for it, don't queue through it. Model a Selenium navigation as *one* request event; let `_driver_lock` remain the concurrency primitive. This directly answers §5.10's "how do we prevent one stalled Selenium operation from blocking all other HDEncode work" — keep the queues separate, share only the counters and the health gate.
- **Health state:** one row per source in a small table, plus `ws_manager.broadcast_sync` for live UI. Persist `state`, `reason_code`, `cooldown_until`, `last_success_at`, `consecutive_failures`. That is enough; see C.
- **Telemetry:** `request_events` table with `url_hash` rather than raw URL (§5.9's own instinct is right — these URLs identify pirated releases; hash them). Retain 7–14 days, not indefinitely.

---

## C. Scope refinement

| Item | Verdict | Why |
|---|---|---|
| **1. RSS-first discovery** | **Split, then keep the second half** | The traffic win is *lazy hydration*, and it does **not** require RSS. Listing pages already yield title text (`_select_posts`, `:721-740`); parse those, test relevance, fetch details only for survivors. That is most of the benefit with none of the feed dependency. Keep RSS as a follow-on, gated on verifying feeds exist (§E). |
| **2. Request broker** | **Keep, reduced** | Revive `network.py` (A.2); wire System A to `SourceConfig` (A.7). Drop `SCHEDULED_SCAN` (A.6). Skip the reservation schemes in §5.7 — plain priority ordering is sufficient and testable. |
| **3. Source-health state machine** | **Keep, much reduced** | The classifier exists (A.3); this is plumbing. Collapse §6.2's 11 states to **5**: `HEALTHY`, `DEGRADED`, `BLOCKED` (with `reason_code` carrying rate-limit vs challenge vs layout), `COOLDOWN`, `UNKNOWN`. `CAPTCHA_REQUIRED`/`RATE_LIMITED`/`LAYOUT_CHANGED`/`HOST_LINKS_UNAVAILABLE` are *reason codes*, not states — your §6.2 note anticipating this is right. `RECOVERING`/`CHALLENGE_WAITING` are transient and don't need persisting. |
| **4. Manual open-and-paste** | **Keep as specified** | Highest reliability-per-line in the document. The user opens the page themselves, satisfies any challenge as a human, and pastes results — ScanHound issues no request at all. §7.4's validation list is sound; add it as written. |
| **5. Browser extension** | **Defer** | It's Item 4 with less typing, at the cost of a pairing-token protocol, a threat model (§8.8), a separate release channel, and per-browser maintenance. Revisit only if telemetry shows manual paste is used often enough to be a real friction point. |
| **6. noVNC handoff** | **Reject** | Two independent reasons, either sufficient. **(a)** Its purpose is to have a human clear an interactive challenge so automation can continue — that's using human presence as a bypass for a control specifically designed to gate bots, and I won't build it. **(b)** It doesn't work anyway: per A.4 there is no persistent profile, so clearance obtained during handoff dies at the next `_recycle_driver` (`:1155`) or process restart. The "same-session" premise is false today. Item 4 covers the same user need — a blocked grab still completes — with no remote-desktop surface in the container. |
| **7. Persistent browser session** | **Keep, rescoped and bounded** | Rescope: this is *creating* profile persistence (A.4), not hardening it. Set a stable `user_data_dir` on the `./data` volume and stop `rmtree`-ing it. Legitimate — a normal browser keeps its cookies, and re-clearing a challenge on every grab is *more* load on the source, not less. **Bounded:** persist the profile for session continuity; do not add fingerprint or automation-detection evasion on top. Also fix the SIGKILL leak — `_kill_stale_chrome` (`:1044-1055`) `pkill -9`s Chrome, so uc's cleanup never runs and temp profiles accumulate. |
| **9. Contact HDEncode** | **Keep — and move it first** | §3 sequences this last; §19 #10 says do it before assuming thresholds. §19 is right. It is the only item that replaces guesswork with fact, and it costs one email. **Prerequisite:** A.5 — build the off switch first, so a "no" can be honoured. |

---

## D. Implementation sequence

Reordered around one principle: *ship the traffic reductions that need no new architecture before building any.*

**Phase 0 — Comply and throttle (hours).** No new subsystems.
1. Real `hdencode_enabled` honoured by `_build_sources` (A.5). Prerequisite for Item 9.
2. `scan_threads` default 10 → 3, and add per-request spacing in `_process_posts` (A.1). Biggest single traffic cut in the plan.
3. Send the Item 9 inquiry.

**Phase 1 — Tell the truth about failures (small).** Item 3, plumbing only: return the classification from `_log_page_diagnostics`, thread the reason code to DB/API/UI (A.3). Deletes the "everything is 'no links found'" problem without a state machine.

**Phase 2 — Lazy hydration (medium).** Relevance test on listing-derived titles before detail fetch (C, Item 1). No RSS, no new data model beyond a `detail_state` column.

**Phase 3 — Broker (medium).** Revive `network.py`; wire System A to `SourceConfig`; add `request_events` + counters. Now telemetry exists to justify thresholds.

**Phase 4 — Health state + banner (small).** 5 states, WS push via the existing hub.

**Phase 5 — Manual paste recovery (medium).** Item 4 as written.

**Phase 6 — Profile persistence (medium).** Item 7 rescoped.

**Later / conditional.** RSS (needs feed verification); extension (needs evidence Phase 5 is a friction point).

Note this inverts the plan's "telemetry first." Full telemetry before *any* reduction means continuing to send 10-thread unpaced bursts while you measure them. Phase 0 is cheap, safe, and reversible; measure after.

---

## E. Risks and unknowns

**The single largest unknown gating Item 1: do the feeds exist and carry stable GUIDs?** §4.6 asks this and the codebase can't answer it — there is no RSS support anywhere and `SourceCapability.RSS` (`sources/base.py:25`) is an unused enum flag no source declares. I'd rather not probe the site myself; it's a one-line `curl` on your side, and everything in §4.7's 12-task list is wasted if the feeds lack GUIDs or don't cover all categories. Verify before scoping.

**Security.** Item 6 rejected partly on attack surface: x11vnc + websockify + a proxied route into the container is a large addition for one workflow. Item 5's pairing tokens (§8.4) are a real protocol needing replay, CSRF and cross-user binding tests — another reason to defer. Item 4 adds only URL validation, and §7.4 already specifies it correctly (reject embedded credentials and local-network destinations — SSRF is the real risk in a paste box).

**Privacy.** Hash URLs in `request_events` (§5.9's own suggestion). Don't persist file-host links in telemetry, and don't save diagnostic HTML by default — it embeds release names and sometimes links.

**Reliability.** One driver serves all sources behind one lock (`download_service.py:1296`); a persistent profile makes it more valuable and its loss more expensive. Ship an explicit "reset browser profile" diagnostic action (§10.8) in the same PR as Phase 6, not later.

**Migration.** Low. The one real hazard is Phase 2's `detail_state`, which needs a sane default for existing rows — use the `_column_migrations` list (`database.py:591-672`), which already swallows duplicate-column errors.

---

## F. Test plan (additions to §15 — all verifiable without touching the live site)

- **Pacing:** assert `_process_posts` issues ≤ N requests/sec with a fake clock. Regression-guards Phase 0, the change most likely to be silently reverted by a future default bump.
- **Classification round-trip:** feed saved HTML fixtures (Cloudflare interstitial, Turnstile iframe, Chrome `ERR_` page, valid page with no matching host) through `scrape_links` and assert **distinct** reason codes reach the DB row. This is the acceptance test for Phase 1 and it needs no network.
- **Off switch:** `hdencode_enabled=false` ⇒ assert **zero** hdencode requests across a full scan, including the background scanner. Directly tests A.5.
- **Hydration:** an item already in Plex must produce **no** detail fetch.
- **Priority:** a user grab enqueued behind 50 background hydrations executes first.
- **429:** a single 429 halts background work and schedules a cooldown; assert no immediate retry (the current listing path retries nothing but also records nothing — both halves need pinning).
- **Paste validation (adversarial):** `file://`, `http://192.168.1.1`, `http://user:pass@host`, `https://rapidgator.net.evil.com`, and a 10 MB paste body.

---

## G. Effort and PR mapping

| Phase | Complexity | Main files | Own PR? |
|---|---|---|---|
| 0 | Low | `config.py`, `scanner_service.py`, `_build_sources` | Yes — ship immediately |
| 1 | Low–Med | `download_service.py`, `downloads.py`, `database.py`, frontend | Yes |
| 2 | Medium | `scanner_service.py`, `database.py`, frontend | Yes |
| 3 | Medium | `network.py`, `sources/base.py`, `scanner_service.py` | Yes |
| 4 | Low | new health table, `api/ws.py`, frontend banner | Fold into 3 if small |
| 5 | Medium | new route, `download_service.py`, frontend modal | Yes |
| 6 | Medium | `download_service.py`, `entrypoint.sh`, compose | Yes |

The plan's 10-PR breakdown (§14) is about right in granularity; the ordering is what I'd change.

---

## H. Recommended first slice

**Phase 0, items 1 and 2 — a working `hdencode_enabled`, `scan_threads` 10 → 3, and spacing between detail fetches.**

It commits you to no architecture, touches three files, is trivially revertible, and plausibly cuts request volume several-fold on its own — because the current behaviour is 10 concurrent unpaced detail fetches for *every* newly-seen post regardless of whether you'd ever want it. If that alone stops the blocking, most of the rest of this document becomes optional, which is the best possible outcome for a plan this large.

Send the Item 9 email the same day, once the off switch works.
