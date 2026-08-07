"""RSS shadow comparison, scheduling, and promotion evidence."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import random
import re
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

_RELEVANT_STATES={"missing","missing_season","upgrade","dv_upgrade"}

# ── Per-feed observation validity ─────────────────────────────────────────
#
# A miss asserts "the listing had a release the FEED did not". That claim is
# only supportable when the feed that should have carried the release was
# actually observed in THIS cycle.
#
# The two normal feeds are declared in sources/hdencode_feeds.py:
#   movies_all -> media_type "movie"
#   tv_all     -> media_type "tv"
#
# An earlier version of this module gated on rss_requests > 0. A 2026-08-06 peer
# review showed that to be unsound, and the production code confirms it:
# poll_cycle computes requests as
#     sum(1 for r in results if r.get("requested"))
# over `feeds = normal + catchup_feeds()`, and poll_feed returns
# {"outcome": "failed", ..., "requested": True} on an exception. So
# rss_requests > 0 is satisfied by a catch-up-only cycle, by an attempted
# request that failed, and by a cycle where one normal feed succeeded and the
# other did not -- in every one of which candidate_urls is wholly or partly the
# persisted snapshot from an earlier cycle rather than a current observation.
# It means "something was attempted somewhere", not "this release's feed was
# validly observed".
_VALID_FEED_OUTCOMES = frozenset({"changed", "not_modified"})

_FEED_FOR_MEDIA_TYPE = {"movie": "movies_all", "tv": "tv_all"}

# Statuses that only exist for a series.
_TV_ONLY_STATES = frozenset({"missing_season"})

# Crawl categories that affirmatively identify a film. Assigned at source
# construction in scanner_service alongside "type": "movie". "search" is
# deliberately absent: a search result carries no type evidence either way, and
# treating it as a movie is how a real TV row reached movies_all.
_MOVIE_CATEGORIES = frozenset({"4k", "remux"})

# A season (and optional episode) marker in an HDEncode slug, e.g.
# ".../will-and-grace-s07-1080p-..." or ".../show-s01e04-720p-...".
#
# POSITIVE TV EVIDENCE ONLY. Its absence proves nothing -- many series filenames
# do not match, and reading "no sNN" as "movie" is precisely the defect a
# 2026-08-06 peer review found. It exists for rows carrying no structured
# evidence, mainly historical ones: hdencode_shadow_misses stored only
# canonical_url, title and status before this branch.
_SEASON_SLUG = re.compile(r"-s\d{1,3}(?:e\d{1,3})?[-/]")


def attribution_evidence(row: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Return (media_type, basis) for the feed that should carry a row.

    media_type is "movie", "tv", or "unknown". basis lists the signals that
    decided it, so a counted miss can be audited after the fact instead of
    re-derived by guesswork.

    EVIDENCE, NOT A GUESS CHAIN. The first version of this function ended with
    "if the url is non-empty, it is a movie", which made "unknown" reachable only
    for an empty url. A 2026-08-06 peer review found that it therefore
    contradicted its own docstring: the docstring argued that guessing movie is
    unsafe because it can suppress a real TV miss, and then the code guessed
    movie for essentially every row. It also dropped the scanner's explicit
    category, which is the authoritative signal.

    THE SIGNALS, and why absence of one is not evidence of the other:

      category         Assigned at source construction (scanner_service): "tv"
                       for the TV Packs listings, "4k"/"remux" for the movie
                       listings, "search" for search results. "search" carries no
                       type evidence at all and must not be read as either.
      is_tv            True is affirmative TV evidence. False is NOT read as
                       movie evidence -- a parser negative means "the TV pattern
                       did not match", not "this is a film".
      season/episodes  Structured series evidence.
      series-only status  e.g. missing_season.
      sNN / sNNeNN slug   POSITIVE TV evidence only. Its absence says nothing:
                       plenty of series filenames do not match the pattern, and
                       that is exactly how a real TV row was being attributed to
                       movies_all.

    CONFLICTS RESOLVE TO "unknown". Neither misattribution direction is safe: a
    TV row checked against a failed movie feed is dropped, and so is a movie row
    checked against a failed TV feed. When signals disagree, the honest answer is
    that we do not know, and "unknown" requires BOTH feeds validated -- so an
    ambiguous row can never be discarded on the strength of one healthy feed.
    """
    tv: list[str] = []
    movie: list[str] = []

    category = str(row.get("category") or "").strip().lower()
    if category == "tv":
        tv.append("category=tv")
    elif category in _MOVIE_CATEGORIES:
        movie.append(f"category={category}")

    # is_tv=True is affirmative TV evidence. is_tv=False is NOT movie evidence:
    # the detail scraper's false value means only "the TV regex did not match",
    # which is the same absence-as-opposite inference removed from the slug. A
    # 2026-08-06 review required this either dropped or explicitly qualified.
    # MediaItem does not currently retain the field, but the helper accepts
    # dicts, so the latent bug was reachable.
    if row.get("is_tv") is True:
        tv.append("is_tv=True")

    season = row.get("season")
    if season is not None and str(season).strip() not in ("", "None"):
        tv.append("season")
    if row.get("episodes"):
        tv.append("episodes")
    if _status_value(row) in _TV_ONLY_STATES:
        tv.append("status_series_only")

    url = str(row.get("url") or row.get("canonical_url") or "").lower()
    if url and _SEASON_SLUG.search(url):
        tv.append("slug_season_marker")

    if tv and movie:
        return "unknown", tuple(["conflict"] + tv + movie)
    if tv:
        return "tv", tuple(tv)
    if movie:
        return "movie", tuple(movie)
    return "unknown", ("no_affirmative_evidence",)


def attribute_listing_media_type(row: Mapping[str, Any]) -> str:
    """media_type only. See attribution_evidence for the reasoning and basis."""
    return attribution_evidence(row)[0]


def feed_observation_valid(media_type: str,
                           normal_feed_outcomes: Mapping[str, str]) -> bool:
    """Whether the feed responsible for ``media_type`` was observed this cycle.

    ``normal_feed_outcomes`` maps a normal feed key to that cycle's outcome. An
    absent key means the feed produced no result at all (not_due, or the cycle
    stopped before reaching it) and is therefore not an observation.

    "unknown" attribution requires both normal feeds, so an unattributable row
    can never be dropped on the strength of one healthy feed.
    """
    outcomes = {str(k): str(v) for k, v in dict(normal_feed_outcomes or {}).items()}

    def ok(key: str) -> bool:
        return outcomes.get(key) in _VALID_FEED_OUTCOMES

    if media_type == "unknown":
        return all(ok(key) for key in _FEED_FOR_MEDIA_TYPE.values())
    key = _FEED_FOR_MEDIA_TYPE.get(media_type)
    if key is None:
        return all(ok(k) for k in _FEED_FOR_MEDIA_TYPE.values())
    return ok(key)


def normal_feed_outcomes_from_results(results: Iterable[Mapping[str, Any]]) -> Dict[str, str]:
    """Reduce a poll cycle's per-feed results to the normal feeds only.

    Catch-up feeds are deliberately excluded: a catch-up request can never make
    a normal-feed comparison valid. That was the first of the review's
    disagreeing cases.
    """
    out: Dict[str, str] = {}
    for result in results or ():
        key = str((result or {}).get("feed") or "")
        if key in _FEED_FOR_MEDIA_TYPE.values():
            outcome = (result or {}).get("outcome")
            if outcome is not None:
                out[key] = str(outcome)
    return out

def canonical_url(value: str) -> str:
    parsed=urlsplit(str(value or '').strip())
    if not parsed.scheme or not parsed.netloc: return str(value or '').strip().rstrip('/')
    path=(parsed.path or '/').rstrip('/') or '/'
    return urlunsplit((parsed.scheme.lower(),parsed.netloc.lower(),path,'',''))

def jittered_interval_seconds(minutes: int, *, jitter_minutes: int=10, rng=None) -> int:
    base=max(15,min(int(minutes),360))*60
    source=rng or random
    offset=source.uniform(-abs(jitter_minutes)*60,abs(jitter_minutes)*60)
    return max(5*60,int(base+offset))

def catchup_required(states: Iterable[Mapping[str,Any]], *, now: Optional[datetime]=None, fallback_hours: int=4) -> bool:
    now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows=list(states)
    if not rows: return True
    for row in rows:
        checked=row.get('last_checked_at')
        depth=row.get('observed_depth_seconds')
        try:
            checked_dt=datetime.fromisoformat(str(checked))
            if checked_dt.tzinfo is None: checked_dt=checked_dt.replace(tzinfo=timezone.utc)
            elapsed=(now-checked_dt.astimezone(timezone.utc)).total_seconds()
        except (TypeError,ValueError): return True
        try: depth_s=int(depth or 0)
        except (TypeError,ValueError): depth_s=0
        if depth_s>0:
            safe_window=max(3600,depth_s-max(7200,int(depth_s*0.25)))
            if elapsed>=safe_window: return True
        elif elapsed>=max(1,min(int(fallback_hours),48))*3600: return True
    return False

def _row_dict(item: Any) -> dict:
    if isinstance(item,dict): return item
    # season/episodes are read for feed attribution: a MediaItem with a season is
    # a series and belongs to tv_all. Without them every listing row would fall
    # back to slug matching, which is all a historical row can offer.
    # category is the AUTHORITATIVE attribution signal and was previously
    # dropped here, so attribution fell back to a slug heuristic that read every
    # non-sNN url as a movie. scanner_service sets it at source construction:
    # "tv" for the TV Packs listings, "4k"/"remux" for movie listings, "search"
    # for search results. Losing it is what allowed a genuine TV miss to be
    # attributed to movies_all and suppressed.
    return {name:getattr(item,name,None) for name in ('url','status','status_text','posted_date','title','season','episodes','category','is_tv')}

def _status_value(row: Mapping[str,Any]) -> str:
    value=row.get('status')
    if hasattr(value,'value'): value=value.value
    return str(value or row.get('status_text') or '').strip().lower().replace(' ','_')

@dataclass(frozen=True)
class ShadowComparison:
    rss_count:int; listing_count:int; duplicate_count:int; feed_only_count:int; listing_only_count:int
    relevant_miss_count:int; rss_requests:int; listing_requests:int; request_reduction_pct:float
    normal_feeds_complete:bool; outcome:str; feed_only:tuple[str,...]; listing_only:tuple[str,...]
    relevant_misses:tuple[dict,...]
    #: Whether the LISTING arm of this comparison is trustworthy, independent of
    #: feed health. None means "not recorded" (cycles written before this field).
    #:
    #: WHY THIS IS SEPARATE, added 2026-08-07 on peer review. `normal_feeds_complete`
    #: conflates two different failures: _rss_normal_feeds_complete() returns False
    #: when a normal feed failed AND when the listing crawl errored. So the stored
    #: outcome "incomplete_feeds" cannot tell those apart, and resolution needs
    #: both authorities separately -- a movie miss can be resolved by a cycle where
    #: tv_all failed, but NOT by one where the listing itself was broken, because
    #: the listing is the other half of the comparison.
    listing_complete:Optional[bool]=None
    # Per-normal-feed provenance for THIS cycle, e.g.
    # {"movies_all": "changed", "tv_all": "failed"}. Persisted so a miss can be
    # attributed retrospectively; without it a re-grade has only a cycle-level
    # boolean, which is why the 2026-07-22..08-05 window cannot be re-graded
    # under attribution at all.
    normal_feed_outcomes:Dict[str,str]=field(default_factory=dict)
    # Listing rows in a relevant state that were NOT booked as misses because
    # the feed responsible for them was not observed this cycle. Kept as
    # evidence: they are real releases seen during a period with no valid feed
    # observation, which is a coverage-observability gap even though it is not a
    # proven miss.
    unattributable:tuple[dict,...]=()
    def as_dict(self): return asdict(self)

def compare_shadow(*, rss_urls: Iterable[str], listing_items: Iterable[Any], rss_requests:int, listing_requests:int, normal_feeds_complete:bool, normal_feed_outcomes: Optional[Mapping[str,str]]=None, listing_complete: Optional[bool]=None) -> ShadowComparison:
    rss={canonical_url(u) for u in rss_urls if u}
    listing={}
    for item in listing_items:
        row=_row_dict(item); url=canonical_url(row.get('url'))
        if url: listing[url]=row
    listing_urls=set(listing); duplicate=rss & listing_urls; feed_only=rss-listing_urls; listing_only=listing_urls-rss
    # THREE-STATE, and the distinction is load-bearing.
    #
    #   None  -> the caller supplies no provenance. Fall back to the
    #            cycle-level conservative rule: both normal feeds completed, so
    #            both were validated. Defaulting to "count nothing" here would
    #            let any caller silently disable miss detection by omission,
    #            which is a worse failure than the bug being fixed.
    #   {}    -> provenance supplied and EMPTY: no normal feed produced an
    #            outcome this cycle (catch-up only, or stopped early). Nothing is
    #            attributable, so nothing is counted.
    #   {...} -> attribute per release.
    if normal_feed_outcomes is None:
        # Decide with the conservative equivalent, but do NOT record it as
        # observation. Persisting {"movies_all": "changed"} here would fabricate
        # evidence that no feed produced. The marker keeps the row non-NULL --
        # so get_hdencode_shadow_summary still trusts the count, which IS
        # correctly filtered -- while stating plainly how it was derived.
        outcomes=({'movies_all':'changed','tv_all':'changed'}
                  if normal_feeds_complete else {})
        recorded={'_derived_from':'cycle_level_completeness',
                  'normal_feeds_complete':bool(normal_feeds_complete)}
    else:
        outcomes=normal_feed_outcomes_from_results(
            [{'feed':k,'outcome':v} for k,v in dict(normal_feed_outcomes).items()])
        recorded=dict(outcomes)
    misses=[]; unattributable=[]
    for url in sorted(listing_only):
        row=listing[url]
        if _status_value(row) not in _RELEVANT_STATES: continue
        media_type,basis=attribution_evidence({**row,'url':url})
        record={'canonical_url':url,'title':row.get('title'),
                'status':_status_value(row),'media_type':media_type,
                # Persisted so a counted miss's attribution can be audited
                # later rather than re-derived by guesswork.
                'attribution_basis':','.join(basis)}
        # PER-FEED VALIDITY. A miss is only a miss if the feed that should have
        # carried this release was observed in this cycle. See
        # feed_observation_valid: catch-up feeds cannot validate a normal-feed
        # comparison, an attempted-but-failed request is not an observation, and
        # a cycle where movies_all succeeded says nothing about a tv_all gap.
        if feed_observation_valid(media_type,outcomes):
            misses.append(record)
        else:
            record['unattributable_reason']=(
                f"{_FEED_FOR_MEDIA_TYPE.get(media_type,'both normal feeds')} "
                f"outcome={outcomes.get(_FEED_FOR_MEDIA_TYPE.get(media_type,''),'absent')}")
            unattributable.append(record)
    reduction=0.0
    if listing_requests>0: reduction=100.0*(listing_requests-rss_requests)/listing_requests
    outcome='success' if normal_feeds_complete else 'incomplete_feeds'
    # A miss asserts "the listing had a release the FEED did not". That claim
    # needs a feed side that actually fetched this cycle. When
    # normal_feeds_complete is False it did not: rss_urls comes from
    # list_hdencode_current_feed_urls(), which reads the persisted membership of
    # the last CHANGED feed snapshot out of the database -- not this cycle's
    # fetch. So listing_only is inflated by everything the feed had merely not
    # fetched yet, and every relevant row in it gets booked as a miss.
    #
    # Two bugs met here. The overwrite below also erased the very label that
    # said the comparison was invalid: `if misses: outcome='relevant_miss'` ran
    # unconditionally, so an incomplete_feeds cycle was relabelled as a cycle
    # that found a real gap.
    #
    # Measured over 2026-07-22..2026-08-05 (300 cycles): 41 cycles had
    # rss_requests=0 while reporting rss_count=100 from the stale snapshot, and
    # they produced 89 of the 150 recorded misses. Grading all 150 against later
    # cycles found ZERO permanent losses -- median catch-up 1.10h, worst 4.06h.
    # The gate was reading feed latency and reporting it as coverage loss.
    #
    # listing_only is still returned and persisted in details_json, so dropping
    # the miss rows discards no diagnostic detail -- only the false claim.
    #
    # The 2026-07-21 audit rule (f5e3c6e) is preserved by ATTRIBUTION, not by a
    # cycle-level proxy. Its requirement was that a degraded cycle must not hide
    # a genuine gap. Per-feed validity honours that precisely: a real movie miss
    # still blocks when tv_all failed, and a real TV miss still blocks when
    # movies_all failed. What is excluded is only the row whose OWN feed was not
    # observed -- where the comparison is listing-vs-stale-snapshot and can
    # prove neither success nor failure.
    #
    # An earlier attempt gated on rss_requests>0 and was refuted: that count
    # spans catch-up feeds and counts attempted-but-failed requests, so it
    # admitted exactly the stale comparisons it was meant to exclude.
    if misses and normal_feeds_complete: outcome='relevant_miss'
    return ShadowComparison(len(rss),len(listing_urls),len(duplicate),len(feed_only),len(listing_only),len(misses),int(rss_requests),int(listing_requests),round(reduction,2),bool(normal_feeds_complete),outcome,tuple(sorted(feed_only)),tuple(sorted(listing_only)),tuple(misses),dict(recorded),tuple(unattributable),
        listing_complete=(None if listing_complete is None else bool(listing_complete)))


# ---------------------------------------------------------------------------
# Miss resolution: was a listing-only release ever acquired?
# ---------------------------------------------------------------------------
#
# THE RULE, decided by Jesse on 2026-08-07: a release counts against RSS only if
# it was NEVER acquired, by any route, with no time limit.
#
# WHAT IT REPLACES. Readiness blocked on `relevant_misses > 0` -- any listing-only
# observation, ever, blocked permanently. The measurement showed those are not
# losses: 99 of 100 were acquired anyway, median about an hour, all through the
# normal feeds. They were late, not lost. Exactly one was never acquired. Under
# the old rule 60 records that all grade GREEN could never pass, so RSS would
# stay in shadow mode however well it worked.
#
# MY RESERVATION, recorded once and not re-argued: with no deadline a release
# acquired three days late counts as a success, so the rule stops measuring the
# latency RSS exists to improve. I offered a 6-hour budget; Jesse chose the
# simpler rule. The lag is still returned per row so it can be reported even
# though it no longer gates.
#
# WHAT IS DELIBERATELY *NOT* WIDENED. "Never acquired" is a claim requiring
# evidence, and so is "acquired". A row we cannot decide either way is neither:
# it is UNDETERMINED and it still blocks. Treating unprovable as fine is the
# fail-open shape that produced two HIGH findings in this same subsystem, and the
# existing grader already gates on "0 RED, 0 PENDING and 0 AMBIGUOUS".

#: A cycle only counts as an observation when both sides genuinely completed.
_RESOLUTION_STATES = ("acquired", "never_acquired", "undetermined",
                      "not_yet_assessable")

#: The only media types a miss row may carry. Anything else is corrupt evidence
#: and cannot be resolved, because the responsible feed is unknown.
_VALID_MEDIA_TYPES = frozenset({"movie", "tv", "unknown"})


def cycle_is_valid_evidence_for(media_type, cycle):
    """May this cycle be used as an observation for a miss of ``media_type``?

    TWO INDEPENDENT AUTHORITIES, both required. Peer review found that round 2 had
    the per-feed rule right in this helper and then never reached it, because the
    database reader filtered cycles on ``outcome in ("success","relevant_miss")``
    while `compare_shadow` stores every aggregate-incomplete comparison as
    ``incomplete_feeds``. A cycle with movies_all=changed and tv_all=failed -- the
    exact case this helper exists for -- was discarded before it ran.

    But simply admitting ``incomplete_feeds`` is not safe either, because
    `_rss_normal_feeds_complete()` also returns False when the LISTING crawl
    errored, and the listing is the other half of the comparison. So:

    1. **the listing arm must be trustworthy**, and
    2. **the feed responsible for this media type must have been observed**.

    ``listing_complete`` carries (1) explicitly. ``None`` means the cycle predates
    that field, and then the conservative cycle-level rule stands in for both --
    which is the same legacy fallback the review accepted, and the same information
    loss: old mixed cycles cannot be re-derived at per-feed precision.

    ``outcomes`` carries (2) as a per-feed map, and the fallback is decided on
    ``is None`` rather than truthiness -- an explicit empty map means "no feed was
    observed", which is NOT the same as "no per-feed data was recorded". Collapsing
    those two was a separate review finding.
    """
    listing_ok = cycle.get("listing_complete")
    outcomes = cycle.get("outcomes")

    if listing_ok is None or outcomes is None:
        # Legacy cycle: nothing finer than the aggregate flag exists.
        return bool(cycle.get("cycle_complete"))
    if not listing_ok:
        # The listing arm is untrustworthy, so this cycle cannot resolve anything
        # however healthy its feeds were.
        return False
    # An explicit empty map means no normal feed was observed this cycle.
    return feed_observation_valid(str(media_type), outcomes)


def classify_miss_resolution(url, media_type, first_seen, cycles):
    """Was ``url`` -- listing-only at ``first_seen`` -- ever provably acquired?

    ``cycles`` is an ordered sequence of mappings with ``at`` (datetime),
    ``listing_only`` / ``feed_only`` (containers of canonical URLs) and
    ``outcomes`` (that cycle's per-normal-feed outcome map). Only cycles strictly
    after ``first_seen`` count, and only those whose relevant feed was actually
    observed for THIS media type.

    Returns ``(state, hours, detail)`` with state in ``acquired`` /
    ``never_acquired`` / ``undetermined`` / ``not_yet_assessable``.

    PER-FEED AUTHORITY, restored 2026-08-07 after peer review. The first version
    admitted an observation cycle only when ``normal_feeds_complete == 1``. That
    is the cycle-level rule this project spent five review rounds REPLACING: a
    cycle where movies_all validated and tv_all failed is perfectly good evidence
    about a movie, and useless about TV. compare_shadow already emits misses on
    exactly that basis -- its own comment says "a cycle where movies_all
    succeeded says nothing about a tv_all gap" -- so filtering resolution on
    cycle completeness threw away legitimate evidence and, worse, dropped
    legitimately-recorded misses out of the gate entirely. That was a false-ready
    path and it was mine.

    The only affirmative evidence of acquisition is a later valid cycle in which
    the feed has the URL and the listing no longer does -- the transition the
    qualification grader treats as definitive. Absence from both sides proves
    nothing: the listing pages away over time.
    """
    valid_later = [c for c in cycles
                   if c.get("at") is not None and c["at"] > first_seen
                   and cycle_is_valid_evidence_for(str(media_type), c)]
    if not valid_later:
        return ("not_yet_assessable", 0.0,
                "no completed observation of this release's own feed exists "
                "after this row yet")

    last_missing = None
    for cycle in valid_later:
        if url in (cycle.get("feed_only") or ()):
            return ("acquired",
                    (cycle["at"] - first_seen).total_seconds() / 3600.0, "")
        if url in (cycle.get("listing_only") or ()):
            last_missing = cycle["at"]
    newest = max(c["at"] for c in valid_later)
    span = (newest - first_seen).total_seconds() / 3600.0
    if last_missing is not None:
        return ("never_acquired", span,
                "observed still missing at a later valid cycle, never acquired")
    return ("undetermined", span,
            "left the listing without ever appearing in the feed, so neither "
            "acquisition nor loss can be proven")


def summarise_miss_resolutions(misses, cycles):
    """Aggregate per-row classifications into the counts readiness gates on.

    ``misses`` is a sequence of mappings with ``url``, ``media_type`` and ``at``.

    NOT_YET_ASSESSABLE NOW BLOCKS, reversed 2026-08-07 on peer review. I had
    excluded it so the gate could pass, and the review showed that is unsafe in a
    way I had not considered: the shadow comparison is recorded only while
    ``discovery_mode == "rss_shadow"`` (background_scanner.py:449). So promoting
    to rss_primary STOPS producing the observations a pending row needs. The gate
    could open on evidence its own promoted mode then destroys, turning
    "temporarily unassessable" into a permanent blind spot.

    The honest way to pass is a frozen cohort -- fix an admission cutoff, collect
    an observation tail, require every admitted row to resolve -- not to make a
    live unresolved row vanish because it happens to be newest.
    """
    counts = {state: 0 for state in _RESOLUTION_STATES}
    rows = []
    worst_lag = 0.0
    for miss in misses:
        url = miss.get("url")
        first_seen = miss.get("at")
        media_type = miss.get("media_type")
        if not url or first_seen is None:
            counts["undetermined"] += 1
            rows.append({"url": url, "state": "undetermined", "hours": 0.0,
                         "detail": "missing url or timestamp"})
            continue
        if media_type is None:
            # PRE-ATTRIBUTION LEGACY ROW. media_type was added by the RSS
            # accounting work, so every miss recorded before that migration has
            # NULL here -- 70 of 72 rows in the live database. Treating those as
            # corrupt blocked the gate on all of them, which is a false BLOCK
            # exactly as bad as the false ready it replaced. I only found it
            # because the measurement swung from 62 acquired to 1.
            #
            # "unknown" is the honest reading: the responsible feed was never
            # recorded, so resolution requires BOTH feeds where per-feed data
            # exists, and falls back to that cycle's completeness where it does
            # not. Conservative without being blind.
            media_type = "unknown"
        elif str(media_type) not in _VALID_MEDIA_TYPES:
            # A value that is present but outside the vocabulary is corrupt
            # evidence, not a legacy gap, and must not be silently coerced.
            counts["undetermined"] += 1
            rows.append({"url": url, "state": "undetermined", "hours": 0.0,
                         "detail": f"invalid media_type {media_type!r}"})
            continue
        state, hours, detail = classify_miss_resolution(
            url, media_type, first_seen, cycles)
        counts[state] += 1
        if state == "acquired":
            worst_lag = max(worst_lag, hours)
        rows.append({"url": url, "media_type": str(media_type), "state": state,
                     "hours": round(hours, 3), "detail": detail})
    counts["worst_acquisition_lag_hours"] = round(worst_lag, 3)
    counts["rows"] = rows
    # not_yet_assessable is INCLUDED here; see the docstring above.
    counts["blocking"] = (counts["never_acquired"] + counts["undetermined"]
                          + counts["not_yet_assessable"])
    return counts
