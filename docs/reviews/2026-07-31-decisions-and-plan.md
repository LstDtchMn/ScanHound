# Decisions and plan — for adversarial review, 2026-07-31

> [!CAUTION]
> **SUPERSEDED — DO NOT EXECUTE FROM THIS FILE.**
> The controlling plan is `2026-07-31-plan-rev2-AUTHORITATIVE.md`.
> This file is retained only as a historical record of what was believed
> before peer review. Its RSS/full-disc reasoning is **wrong** and was
> retracted.

**Author:** Claude · **Reviewer:** ChatGPT · **Arbiter:** Jesse
Equal peers. Merge, deploy, force-push, production settings and feature
enablement remain Jesse-only. Auto-rename and auto-grab stay off.

22 decisions were collected from Jesse in one sitting, deliberately **before**
any work began, to stop implementation running ahead of intent. Nothing below
has been built yet. This document is the plan, not a report of work done.

Every figure was measured on 2026-07-30/31. None is carried from an earlier
document or a code comment — a previous round's headline finding rested on a
stale number copied out of a config comment and was refuted twice.

---

## What I most want attacked

1. **§A — my own hypothesis about the RSS miss count, which I now think may be
   backwards.** I would rather be corrected here than anywhere else.
2. **§B — the decision to ignore the external sender entirely.**
3. **§C — the sequencing.** Twelve work items; I may have the dependencies wrong.
4. **§D — the auto-rename supervised run.** It touches real media, under a
   feature paused for a reproduced data-loss defect.

---

## §A The RSS asymmetry, and a correction to my own reasoning

**Established facts (measured):**

```
cycles 163/20   days 9.63/7   misses 97 (was 81 ~18h earlier)
request reduction 89.18%   feeds_healthy True   ready False
```

Misses rose 81 → 97 in about 18 hours. Not a settling tail; the rate holds.
Every miss is a `listing_only` item. `rss_count` is **exactly 100 in all 170
sampled cycles**, while the crawl surfaces ~3.5 items/cycle.

**The asymmetry:** the listing path excludes full-disc `[BD]` releases *before*
detail fetch (shipped 2026-07-30, verified in production). The RSS path has **no
such exclusion** — `hdencode_rss_service.py:322` → `ingest_hdencode_feed()`.

**My original claim to Jesse:** full-disc releases were probably *inflating* the
miss count, so the 97 was unfair to RSS.

**Why I now doubt it.** If RSS ingests full-disc and the listing excludes them,
RSS sees *more*, not less. Those releases should surface as `feed_only`, not as
`listing_only` misses. My claim only holds if the comparison set is built from
**raw listing URLs captured before the exclusion runs** — in which case
full-disc appears on the listing side and, absent from the feeds, counts as a
miss.

I have not read the comparison-set construction, so **I do not know which is
true**, and I retracted the claim to Jesse rather than let it stand.

**Questions for review:**

- Is there a reading of a constant `rss_count = 100` other than a fixed-length
  feed window? Could it be an artefact of the collector rather than the feed?
- Two feeds (`movies_all`, `tv_all`) against three crawled sources (4K Movies,
  Remux Movies, TV Packs). Is a category-coverage gap the parsimonious
  explanation for a miss rate that does not decay?
- **A gate that publishes a verdict while one of its checks is erroring** —
  `app_readiness` has returned `Connection refused` for the entire 9.63 days,
  because the collector calls host `127.0.0.1:9721` and no host port is
  published. Is that fail-open? I think yes, and that it should refuse to grade
  rather than grade partially. Decision 12 fixes the address; I want the
  stronger behaviour argued.

---

## §B Security decisions (external email, 2026-07-30)

An unsolicited email asked whether a "cloudflare bypass piece" in the public
repo was live. **Established, not assumed:**

- The literal string is `CLOUDFLARE_BYPASS`, a capability flag in
  `backend/sources/base.py` meaning "fetch this site with a real browser". True
  for one source.
- ScanHound **does not solve challenges**. `_wait_past_cloudflare()` gives up on
  the authoritative `cf-mitigated: challenge` header. Zero references to any
  captcha-solving service anywhere in the repo.
- `undetected-chromedriver` 3.5.5 is installed but the live adapter is
  `selenium_chromium`. The stealth driver is a retained rollback path.
- This is all *outbound* scraping of third-party sites. It has nothing to do
  with the *inbound* Cloudflare Access protecting Jesse's hostname. The email
  conflates the two.

**Two findings fixed** (`41d0193`, unmerged): `/docs` and `/openapi.json`
answered **HTTP 200 unauthenticated** east-west while `/results` correctly
returned 401; and `_within()` compared lexically normalised strings, so a
symlink inside the served root escaped containment. 17-payload traversal corpus:
16 contained (absolute `/etc/passwd`, repeated `../`, encoded forms,
sibling-prefix), symlink the sole escape. Reverting the fixes fails 9 tests.

**Secret scan:** gitleaks over 703 commits returned 2 findings, **both false
positives** — the same value, the public GPG fingerprint of the official Python
Docker image maintainer. No `.env`, `config.json` or database was ever
committed. **No API keys exposed.**

**Jesse's decisions:**
- **Ignore the sender entirely.** No reply, not even a holding response.
- **Accept the topology disclosure** (production hostname, NAS server and share
  names, drive letters) rather than scrub or rewrite history — the hostname is
  public via Certificate Transparency regardless, and none of it is a credential.
  **Add a pre-commit secret scanner instead**, to prevent the next one.

**Questions for review:** Is silence the right call if the sender is a genuine
researcher who might disclose publicly instead? And is "accept the disclosure,
prevent the next" defensible, or complacent?

---

## §C The plan

**Jesse's actions:** merge + deploy `41d0193` (security) and `2b29896` (4K
filter) in one rebuild; merge the mount branch; switch Windows default terminal
to Console Host so the watchdog windows can be suppressed.

**My work, in dependency order:**

| # | Item | Why here |
|---|---|---|
| 1 | RSS full-disc symmetry | Correctness regardless of §A |
| 2 | **Read the miss-comparison logic** | Must precede any claim about the 97 |
| 3 | RSS miss analysis — the 97 titles | Decides whether RSS is fixable |
| 4 | Rename failure analysis | **69 of 158 jobs (44%) failed** |
| 5 | Blind readiness check fix | Small; unblocks honest grading |
| 6 | Secret scanner + plaintext Gotify token | Decision 3 |
| 7 | Finish audit: routes, secrets, header trust | Highest remaining unknown |
| 8 | TV resolution filter + 720p chip | 228 movies + 230 TV unfilterable |
| 9 | Surface the full-disc setting in the UI | Visible and reversible |
| 10 | #192 RSS criterion spec correction | Doc is now wrong |
| 11 | #184 scan metrics, #185 HDR10+ labels | #184 would have caught full-disc in a day |
| 12 | Documentary design pass | Measured: **48 of 67 in-scope missing (72%)** |
| 13 | Auto-rename supervised-run plan | See §D |

**Step 2 is new** and inserted because of §A. I originally had the miss analysis
immediately after the symmetry fix; that ordering assumed I understood the
contamination direction, and I did not.

**Question for review:** is this order right? My instinct is that #4 (rename
failures) outranks most of it — it is the feature that moves real files, it is
paused for a data-loss defect, and a 44% historical failure rate is unexplained.

---

## §D Auto-rename — the one that can destroy data

Auto-rename stays **off**. Jesse chose "plan a supervised manual run soon": a
handful of files, watched, nothing unattended.

The concern I want tested: the feature is paused because of a **reproduced**
TOCTOU data-loss defect in file placement, and separately 44% of all jobs ever
attempted failed. A supervised run is safer than automation but is still real
files on real disks.

**Questions for review:** should the failure analysis be a hard gate before any
supervised run touches a file? Should a supervised run operate only on copies
until the placement defect has a proven fix? What evidence would you require
before letting this feature move one real file?

---

## §E Documentary support — measured, and a trap found

Jesse asked for a documentary category. Rather than design from the request, I
measured 90 releases from `hdencode.org/genre/documentary/` against the
catalogue:

| Resolution | Sampled | Missing | Rate |
|---|---|---|---|
| 2160p/4K | 19 | 10 | 53% |
| 1080p | 48 | 38 | 79% |
| 720p | 23 | 22 | 96% (no 720p crawl — out of scope) |

**Excluding the 720p confound: 48 of 67 in-scope releases (72%) never entered
the system.** The feature is justified by evidence, not by preference.

**The trap:** the genre page uses a different template — no `article`, `.post`
or `.type-post` elements, 30 `h5` tags instead. The existing parser returns
**zero** against it, silently. Building the crawl target without checking would
have shipped something that ran cleanly and found nothing — the exact failure
shape that hid full-disc releases for months.

Also: `_effective_category()` currently guesses "TV if it has a season, else
4K". A documentary is a movie without a season, so it would be silently labelled
4K unless the crawl tags it explicitly.

**Caveat I cannot exclude:** the catalogue holds 999 URLs and prunes, so some
"missing" items may have been seen and aged out. That could soften 72%; it
cannot explain it away.

---

## §F Corrections I made this round, disclosed

- **§A above** — retracted my own contamination hypothesis before Jesse acted on it.
- Told Jesse a stale scheduled task was "failing, doing nothing" from its name
  and exit code; he authorised deleting it. Reading it first showed exit 3 was
  the collector's **stop-condition signal** and the task was the only thing
  tracking RSS readiness. Deleting it would have destroyed the mechanism behind
  §A. Retracted before acting.
- Diagnosed a desktop popup as a classic console window, shipped
  `-WindowStyle Hidden`, and told Jesse it was fixed — **on a surface I cannot
  observe**. Windows Terminal ignores that flag. I then blamed the wrong task;
  Jesse identified it was not mine ("every couple of minutes" — mine repeats
  every 5; the culprit was a 2-minute Docker port watchdog).
- Reported a stale Disk 9 hardware risk; Jesse corrected that it is not involved
  with Plex or ScanHound. Dropped.
- Wrote `-Repo` for an installer parameter actually named `-SourceRepo`.

The pattern worth challenging: **three of these are me asserting a conclusion
before verifying it on the surface where it was checkable.**

---

> **SUPERSEDED 2026-07-31** by `2026-07-31-plan-rev2-AUTHORITATIVE.md`
> following ChatGPT review of `ade5348` (verdict: revision required).
> Retained only as the record of what was believed before review.
> Do not execute from this file.
