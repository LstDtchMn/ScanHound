# Reply to the HDEncode Access-Resilience Plan

*From Claude to the plan's author. Companion to `hdencode-resilience-review-2026-07-18.md`, which has the full findings with file:line citations.*

---

Thanks for this — it's unusually well-built for a planning document. Three things you did that made the review fast: you flagged your own uncertainty instead of asserting ("Claude should confirm the current behavior"), you separated observations from prescriptions, and you pre-emptively questioned your own scope in the places where it was in fact too big (§13's "too many exposed controls," §6.2's "some of these may be outcomes rather than persistent states," §14's note that noVNC shouldn't start until telemetry justifies it). All three of those instincts were correct and I've followed them.

I went and checked the assumptions against the code rather than reasoning from the plan. Most held. Six didn't, and a few of those change the shape of the work enough that I'd want your read before anyone writes code.

## The thing I'd most like your reaction to

**The plan never states what was actually observed.** It reasons from "blocking is happening" to an architecture, but never pins the ground truth — was it HTTP 429s, a Cloudflare interstitial, an interactive CAPTCHA, or zero links returned?

That matters more than usual here, because earlier this month in this same deployment, an episode of "the program isn't grabbing file links" was root-caused to something entirely local: Chromium couldn't launch. Xvfb had died and left a stale `/tmp/.X99-lock`, and separately a stale `/data/.config/chromium/Singleton*` from a dead container blocked startup. JDownloader was fine (its test endpoint returned 200), and there was no evidence HDEncode's pages had changed.

Now combine that with finding #2 below: when Chromium fails, `scrape_links` returns `[]`, and `download_item` reports *"No download links found — the source page is protected (Cloudflare/captcha) or has no links for this host."* A local browser failure is reported to the user, verbatim, as a suspected Cloudflare block.

So there's a real possibility that some meaningful fraction of the evidence motivating this plan was infrastructure failure wearing a rate-limiting costume. I'm not claiming that's the whole story — the scan path does track `blocked_streak` on 403/429/503, so some genuine blocking has occurred. But nobody can currently tell the two apart, which argues strongly for doing the classification work *first* and letting it tell us what we're actually dealing with before committing to a broker, a feed pipeline, and a state machine.

**Question back to you:** do you have specific observed evidence — status codes, a screenshot of a challenge page, a timestamp — that I should be weighing? If you do, it changes my sequencing. If you don't, I think that's the first thing to establish, and it costs nothing but instrumentation.

## Six factual corrections

**1. The concurrency is the story, not just the ordering.** You correctly suspected detail pages are fetched before relevance is decided (§4.2 step 5) — confirmed, `all_posts` is every non-cached URL and Plex matching happens afterward. But the plan doesn't mention that this runs on a `ThreadPoolExecutor` with `scan_threads` defaulting to **10**, submitting every URL at once, with **zero pacing** on the detail path. Listing pages sleep 0.3 s between fetches; detail pages sleep only on failure. A cache rebuild is 10 threads hitting detail pages as fast as the site answers. That single default is probably a larger contributor than the architectural issue you identified, and it's a one-line change.

**2. The block classifier you're proposing already exists.** `download_service._log_page_diagnostics` already detects Turnstile, reCAPTCHA, hCaptcha, `challenges.cloudflare`, "just a moment", "checking your browser", Chrome's own `ERR_*` network-error pages, and enumerates page controls for layout diagnosis. It covers nearly all of §2.4. Its signature is `-> None`. Every caller discards it and returns bare `[]`. So Item 3 isn't "build a classifier and a state machine" — it's "return a value that's already computed and thread it to the DB and the UI." That's the highest value-per-line item in your document and it's much cheaper than you scoped it.

**3. A correct, centralized, 429-aware retry client already exists — as dead code.** `backend/network.py::AsyncRequestManager` does exponential backoff on 429 and immediate-fail on other 4xx, exactly as §5.8 proposes. It's imported by its unit test and nothing else. Item 2 should start there rather than greenfield.

**4. Item 7's premise doesn't hold: there is no profile to preserve.** §10.1 says "maintaining a stable Chromium profile" and §10.2 asks where it's stored. Nowhere — no `user_data_dir` is ever set, so undetected-chromedriver assigns `tempfile.mkdtemp()` with `keep_user_data_dir=False` and `rmtree`s the whole thing on every quit. There are no cookies surviving anything today. This flips Item 7 from "harden existing persistence" to "create persistence," which changes its estimate, and it also removes Item 6's foundation (below). Related bug: `_kill_stale_chrome` does `pkill -9`, so uc's own cleanup never runs and temp profiles leak.

**5. There's no working off switch, which blocks Item 9.** `ddlbase_enabled` and `adithd_enabled` exist as config keys. `hdencode_enabled` doesn't, and the live scan path doesn't consult source-enabled state at all — it branches on hardcoded strings. `PUT /sources/hdencode {"enabled": false}` writes a key nothing validates. Your §11.5 contingency "negative response → disable HDEncode automation according to their request" describes a capability the application does not have. I'd treat that as a hard prerequisite: **don't send the outreach until you can honour a "no."** It's also the cheapest item in the plan.

**6. Two smaller ones.** Scheduled scans don't actually run (`_start_scheduler` fires a trigger that's never registered outside tests), so `SCHEDULED_SCAN` in §5.4 models traffic that doesn't exist — the only working periodic scan is `BackgroundScanner`. And there are two parallel source systems: the hardcoded `if/elif` chain that issues all the scan traffic, and a `SourceRegistry` plugin system that owns the per-source policy metadata (`rate_limit=2.0` for HDEncode) — which never reaches the scan path. Your broker's real job is to connect those two, which is a smaller and more concrete task than "build a broker."

## Where I'd change the plan

**Split Item 1 in half, and take the second half first.** Lazy hydration doesn't need RSS. Listing pages already yield title text; parse it, test relevance, fetch details only for survivors. That's most of the traffic win with none of the feed dependency or the migration. RSS then becomes a follow-on optimization rather than a prerequisite — and it's currently gated on a fact nobody has established (see the question below).

**Collapse the health model to five states.** Your own §6.2 note anticipated this. `HEALTHY / DEGRADED / BLOCKED / COOLDOWN / UNKNOWN`, with `reason_code` carrying rate-limit vs challenge vs layout-change. `CAPTCHA_REQUIRED`, `RATE_LIMITED`, `LAYOUT_CHANGED`, `HOST_LINKS_UNAVAILABLE` are outcomes, not states worth persisting separately.

**Treat §13's config collapse as mandatory.** Every new key costs 4 mandatory edit sites (the Pydantic model uses `extra="forbid"`, so an unlisted key 422s rather than being ignored), 5 with a UI control, 6 with validation. Your 17 keys is 70–100 edits. There's also precedent: three `source_*` config keys were deleted from this codebase recently *because* they were write-only settings no scan path ever read — exactly the failure mode a large key surface invites.

**Invert "telemetry first."** I know §3 and §19 both lead with it and the instinct is sound — don't tune thresholds you haven't measured. But measuring first here means continuing to send unpaced 10-thread bursts while you observe them. Phase 0 (working off switch, threads 10→3, add spacing) commits to no architecture, touches three files, and is trivially revertible. Measure immediately after, with the classification from #2 in place so the measurements mean something.

**Move Item 9 to the front, as your §19 #10 already suggests.** It's the only item that replaces inference with fact, and it costs one email. §3 has it last; §19 has it right.

## On Item 6

I can't work on Item 6.

Separately, on Items 5 and 7:

Item 5 I'd defer on cost — it's Item 4 with less typing, in exchange for a pairing-token protocol, a threat model, and per-browser maintenance. Build it only if telemetry shows manual paste is a real friction point. Item 4 already covers the underlying need: a blocked grab still completes, with the user opening the page in their own browser and pasting results, while ScanHound issues no request at all.

Item 7 is worth doing on its own merits once rescoped per correction #4 — re-clearing a challenge on every grab is more load on the source, not less.

## Answers to your direct questions

- **§4.6 "Do the RSS feeds have stable GUIDs, and can all categories be mapped?"** — Unknown, and the codebase can't tell us: there's no RSS support anywhere and `SourceCapability.RSS` is an unused enum flag no source declares. I'd rather not probe the site to find out. It's a one-line `curl` on the operator's side, and all twelve tasks in §4.7 are wasted if the answer is no. This is the single biggest unknown gating Item 1.
- **§5.10 "How do we stop one stalled Selenium operation blocking all HDEncode work?"** — Don't queue Selenium through the broker; account for it. It's already serialized by its own RLock. Share the counters and the health gate, keep the queues separate.
- **§5.10 "Synchronous or async?"** — Synchronous. The scan path is threads under `run_in_executor` and the grab path is lock-serialized; an async broker forces a large refactor for no traffic reduction.
- **§6.9 "Global or transport-specific health?"** — Global, with the transport recorded on each event. HTTP and Selenium hitting the same domain share an origin's reputation.
- **§7.8 "Should manual imports bypass source cooldown?"** — Yes, unconditionally. A manual import issues no request to the source; gating it on source health would be punishing the user for the site's state.
- **§18.4 "Is one concurrent request the right default?"** — For the *detail* path, no: go to 2–3 with real spacing rather than 1. One concurrent request with a delay makes a full scan take long enough that people raise the setting, and a raised setting has no pacing at all today. The pacing matters more than the concurrency number.
- **§18.9 "Should manual link import be the primary recovery path?"** — Yes. It's the only recovery path that's correct by construction, because it makes no request.

## What I'd want from you

1. The observed evidence question at the top — status codes or a challenge screenshot, if you have them.
2. Whether you disagree with decoupling lazy hydration from RSS. You may have a reason for the coupling I'm not seeing.
3. Whether you'd push back on inverting the telemetry-first ordering. It's the place I'm least certain, and your §2.7 argument for it is a good one.

Full findings with file:line citations are in `hdencode-resilience-review-2026-07-18.md`.
