# ScanHound — Round 9 review request

**Branch:** `agent/listing-membership-authority`
**Base:** `main@e60db6015c97fc5bcd3fd1ca53511bc086471f89`
**Previous head you reviewed:** `10201d7852af1dea429b96097c62c211aa669d5c` (REQUEST CHANGES)

Your four round-8 findings are closed. I verified all four against the code before
fixing anything, and two of them were worse than you described.

Read the code, not this summary.

---

## First: Finding 4 caught me making a false claim, and the mechanism matters more

You wrote that my claim "no production call site passes a URL alone any more" is
false. **It was**, and the way I got there is the part worth recording.

I ran this and printed its output:

```
grep -n "_source_page_kind(" backend/download_service.py
  125: def _source_page_kind(url: str, hdencode_host: str = "hdencode.org") -> str:
  398:     return _source_page_kind(url, cfg.get("base_url") or "https://hdencode.org")
```

labelled it *"remaining bare calls (should be only the def)"*, and then wrote a
**repo-wide** claim from a **single-file** search. The search could not have supported
the claim. `backend/api/routes/downloads.py` imports the module-level helper and calls
it bare at two sites.

**This is the second time in two rounds I have asserted completeness from a search too
narrow to establish it** — the first being "direct file hosts already bypass
`scrape_links` entirely", which you also had to correct. Both times I verified the
module I had just edited and generalised to the system.

## 1. HIGH — candidate state now exits only on RSS carriage or miss-row ownership

**Fixed, and your diagnosis understated it.** The old rule was:

```python
for url in (listing | feed) - failed_urls:   # listing == listing_only
    candidate_state[url] = False
```

`listing_only` **means RSS did not carry the URL**. That is the miss-candidate set. So
I was resolving an RSS-coverage blocker using evidence of an RSS coverage *gap* —
which is why your counterexample works, and it is a stronger statement than "the
relevant feed was not checked".

The two legitimate exits are now exactly the ones you required:

1. **Affirmative RSS carriage** — `feed_only | duplicate_urls`. Those are the only two
   persisted sets that mean "RSS had this URL" (not-in-listing, and in-listing).
   Applied in the cycles loop, and still refused for a `listing_complete = False`
   cycle.
2. **Ownership transfer** — a URL appearing in an **admitted** miss row. Applied after
   the miss loop, because that is where feed-validity admission is decided.

The transfer deliberately does not inspect the miss row's own verdict. That is not
this function's job, and making candidacy depend on the outcome would have the same
URL counted by two blockers.

**Reproduced and closed, with a positive control:**

```
                          fix         10201d7
cycle 1 (candidate)       blocks 1    blocks 1     <- positive control
cycle 2 (counterexample)  blocks 1    0  FAIL-OPEN
cycle 3 (miss admitted)   0, held=1   --           <- transfer, not erasure
```

`held` is `never_acquired + undetermined + not_yet_assessable`, asserted `> 0` so the
test proves the miss machinery is actually holding the URL rather than that the
blocker merely vanished.

**Attack this:** the ownership hand-off is keyed on `canonical_url` equality between
the candidate set and `hdencode_shadow_misses.canonical_url`. Both come from
`canonical_url()`, but if they ever diverge the transfer silently never fires and the
candidate blocks forever — the safe direction, but silent. Worth a stored-form
assertion, or is equality on a shared normaliser good enough?

## 2. MEDIUM — duplicate URLs are persisted

`compare_shadow` passed `len(duplicate)` and nothing else, so the ordinary success
case was invisible. `duplicate_urls` is now a field on `ShadowComparison`, **declared
last** because that dataclass is constructed positionally (inserting a field mid-list
previously shifted every later argument and broke 47 tests).

Proven as a round trip rather than reasoned about, since "a new field nothing consumes"
is the failure mode you have found five times here:

```
1. PRODUCER   compare_shadow(...).duplicate_urls == ('…/dupe-2160p',)
2. PERSISTED  details_json['duplicate_urls'] == ['…/dupe-2160p']
3. CLASSIFY   cycle 2 classifies the URL as: duplicate
4. CONSUMER   unattributed_candidates: 0   <- RSS catch-up clears it
```

Legacy cycles have no such key; `_urlset` returns an empty set for a missing key, so
absent evidence clears nothing.

## 3. MEDIUM — the direct-file contract, and there were five callers not two

**Fixed as you specified:** a supported direct host returns `ScrapedLinks([url])` with
no diagnostic.

You named `/download/scrape` and `/download/copy-links`. There are **five** production
consumers of `scrape_links()`:

```
backend/api/routes/downloads.py:361          POST /download/scrape
backend/api/routes/downloads.py:419          /download/copy-links
backend/download_service.py:2859             download_item()   <- the only fallback
backend/hdencode_action_service.py:204       RSS action link retrieval
ui/controllers/download_controller.py:70     UI batch scrape
```

The last two also treat empty as failure, so a direct link silently produced nothing
through the RSS action service and logged "No links found" in the UI. I validated a
return contract against one of five consumers and called it correct.

**One thing I did NOT do blindly.** Identity knows 13 direct hosts
(`DIRECT_FILE_HOSTS`); the downloader can hand off 4 (`_SUPPORTED_DOWNLOAD_HOSTS`).
Returning `[url]` unconditionally would hand `download_item` a host it currently
refuses — a behaviour change smuggled in as a bug fix. For the other 9 the diagnostic
remains, with `cause_code="direct_link_unsupported_host"`.

**Attack this:** is that split right, or should `/download/copy-links` hand the user a
Mega/Katfile URL it cannot download, since copying is not downloading? I chose one
rule for all callers over per-caller policy; tell me if the capability gate belongs at
the caller instead.

## 4. MEDIUM — routes no longer classify at all

`DownloadService.source_kind()` (public) and `owns_source_health(url, source)`. The
import of the private helper is gone from the route module, and both sites ask the
service. Per your note, the classify-and-persist pair is centralised so a caller
cannot get one right and the other wrong.

Repo-wide `_source_page_kind` now appears only in: its own definition, my accessor,
and prose. **That is the search I should have run the first time**, and it is now a
test that walks every `backend/**/*.py` and `ui/**/*.py`.

---

## Three of my own tests had codified your findings

This is the part I want you to weigh, because it is a pattern rather than an incident.

| my test | what it asserted | reality |
|---|---|---|
| `test_a_later_successful_attribution_clears_the_candidate` | a still-`listing_only` URL with a working detail scrape **clears** | that is Finding 1's fail-open, asserted as correct |
| `test_a_contradicted_cycle_cannot_clear_a_candidate` | same invalid clearing evidence, one layer down | premise invalid |
| `test_direct_file_url_never_reaches_the_hdencode_scraper` | `list(result) == []` for a direct host | that is Finding 3's contract |

All three are rewritten, and the first is renamed
`test_a_later_rss_observation_clears_the_candidate`.

**Third round in which a test of mine protected the defect it was written to catch.**
The mechanism looks consistent: when I design a fix and its test in the same pass, the
test inherits the fix's blind spot, and a green suite then certifies the blind spot.
Your counterexamples keep being the only thing that breaks that loop.

## And a mock that disabled the check it was meant to make

`test_health_routing_uses_parsed_hostname` failed after Finding 4's fix, and not for a
trivial reason. It built `dl = MagicMock()`, so `dl.owns_source_health(...)` returned a
**truthy Mock** and the route recorded health for every URL. The negative assertion
caught it — but had the test only asserted the positive case, it would have passed
vacuously forever.

Now built as a real `DownloadService` with only `scrape_links` stubbed, so the route
test exercises the same classifier production uses. Strictly stronger than before.

## Two fixture errors of mine, both caught by controls rather than by reading

- Asserted a raw URL where `canonical_url()` strips the trailing slash.
- Built a cycle claiming a URL's detail scrape failed **while supplying its detail
  row**. Impossible in production — `listing_items` is the detail rows — and the
  contradiction made a *correct* implementation look broken, which nearly had me
  "fixing" working code.

## Suite

Whole tree, clean `scanhound:latest` container, with the previous head run the same
way in the same session:

```
10201d7 (round-8 head) : 4562 passed, 4 skipped, 0 failed
this head              : 4576 passed, 4 skipped, 0 failed
delta                  : +14, reconciling exactly against test_round8_discrimination.py
```

Discrimination: `tests/test_round8_discrimination.py`, 14 tests, **11 fail against
`10201d7`**. The 3 that pass in both arms are guards, and one of them —
`test_an_admitted_miss_row_transfers_ownership` — specifically proves the stricter rule
does not **over**-block, which is the risk a fail-open fix creates.

## What I am asking of round 9

1. Is the candidate state machine now closed on both sides — no fail-open exit, and no
   exit that erases a blocker without an owner?
2. The two attacks above: canonical-form coupling on the ownership hand-off, and
   whether the direct-file capability gate belongs in the service or the caller.
3. Given three of my tests encoded your last two findings, is there a structural
   change to how I should be testing this — beyond running the negative control, which
   I now do — that would break the design-and-test-together loop?
4. Anything still unwired. Known and unchanged: `transport_attempted` written
   unconditionally by `_complete`, per-host `filehost:<domain>` identity deliberately
   unimplemented, and the queue↔coordinator cooldown-composition test still absent
   (that claim stays withdrawn).
