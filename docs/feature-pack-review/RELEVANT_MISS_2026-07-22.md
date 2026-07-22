# Relevant-miss incident — 2026-07-22

**Status:** open finding, seeking ChatGPT's second read. No production change made in
response to this finding; nothing was auto-grabbed (auto-grab has been off the entire
window). RSS-correction code deployed at `5012ea2` (main), image `sha256:c62a131f…`,
remains the code these misses were observed under. Note: production has since been
redeployed again (`sha256:a361100…`, 2026-07-22T19:15Z) for an unrelated workstream (a
4K metadata inventory feature); the RSS shadow-mode config, DB integrity, and this
collector were verified intact and undisturbed by that deploy.

**Running tally: 3 relevant misses across 12+ cycles, all self-resolved within one
polling interval.** No permanent RSS coverage gap has been observed.

## What the collector detected

The unattended evidence collector (`docs/feature-pack-review/qualification-evidence/collect_shadow_evidence.py`,
running the corrected `05_shadow_evidence.py`) recorded mandatory stop conditions at
`2026-07-22T10:57:00Z` (`relevant_misses=2`) and `2026-07-22T16:57:00Z`
(`relevant_misses=3`, one additional miss from cycle 12). Raw evidence:
[`incident-2026-07-22-relevant-miss/05_shadow_evidence_20260722T105700Z.json`](qualification-evidence/incident-2026-07-22-relevant-miss/05_shadow_evidence_20260722T105700Z.json)
(SHA-256 `78292df31fb718126cad66e32a078de02bec426787c9ecac5aca601fc11d2507`).

## Root cause (traced against the raw cycle rows)

**Incident 1 (cycles 7–9):** two file variants of a single release — **Masters of
the Universe (2026), 2160p/UHD** (a DV+HDR encode and a non-DV encode, same
title/source/group).

| Cycle id | `completed_at` (UTC) | RSS has it? | Listing has it? |
|---|---|---|---|
| 7 | 07:48:19 | No | **Yes → recorded as relevant miss** (`outcome=relevant_miss`) |
| 8 | 08:58:38 | Yes (`feed_only`) | No (fell off the scraped listing window) |
| 9 | 10:06:13 | Yes (`feed_only`) | No |

hdencode's live listing page showed the release before their RSS feed carried it;
RSS caught up on its own roughly 70 minutes later, with no code intervention. By the
time RSS had it, the item had already scrolled past the listing scraper's shallow
window (`background_scan_pages=3`), so it never re-appears as `duplicate` — it
shows as `feed_only` in cycles 8/9 instead.

**Incident 2 (cycle 12):** one release — **"Dateline: Unforgettable" S07**, 1080p.

| Cycle id | `completed_at` (UTC) | Outcome |
|---|---|---|
| 12 | 13:42:07 | **Yes → recorded as relevant miss** (1 miss) |
| 13 | 14:59:11 | `success`, 0 misses |
| 14 | 16:02:29 | `success`, 0 misses |

Same pattern: self-resolved within roughly one polling interval, no code
intervention. This is now two independent incidents showing the identical
mechanism — a real, but transient, RSS-vs-listing publish lag on hdencode's side.

## Confirmed NOT a defect in either correction package

`backend/hdencode_shadow.py::compare_shadow` — the function that classifies a URL as
a relevant miss — has not been touched since the original RSS build (`a55b2e5`);
neither the readiness-aggregation correction (`f5e3c6e`) nor the final follow-up
(`3a5706a`) modified it. Its logic is a straightforward set-difference (`listing_urls
- rss_urls`, filtered to `status in {missing, missing_season, upgrade, dv_upgrade}`)
and classified this case correctly: the listing page genuinely had the release before
RSS did.

## What this means under the corrected readiness logic

Per the readiness-aggregation correction, relevant misses are summed across every
cycle in the window's lifetime by design (a miss must not be forgettable, even from
an otherwise-incomplete cycle). Practically, this means **the window that started
2026-07-21T23:07Z cannot report `ready=true` again** without either a formal reset
or a revisit of what counts as a disqualifying miss.

## The question for review

Is a miss that **self-resolves within one polling interval** (the RSS feed
eventually carries the exact same release, unprompted) equivalent to a **permanent**
miss for readiness purposes, or does the "relevant miss" stop condition need a
distinct sub-classification for transient publish lag vs. genuine coverage gaps?

With two independent incidents now observed (cycles 7–9 and cycle 12), both showing
the identical self-healing mechanism and neither showing any content that RSS never
eventually caught, this is starting to look less like a rare fluke and more like a
real, bounded characteristic of hdencode's RSS feed — but 2 events in 12 cycles is
still a small sample. Jesse's decision remains to keep the window running unmodified
and gather more data before anyone decides. No code or window change has been made
in response to either finding; this is a policy question, not a bug, and stays open
pending discussion.
