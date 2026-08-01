"""RSS shadow comparison, scheduling, and promotion evidence."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import random
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

_RELEVANT_STATES={"missing","missing_season","upgrade","dv_upgrade"}

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
    return {name:getattr(item,name,None) for name in ('url','status','status_text','posted_date','title')}

def _status_value(row: Mapping[str,Any]) -> str:
    value=row.get('status')
    if hasattr(value,'value'): value=value.value
    return str(value or row.get('status_text') or '').strip().lower().replace(' ','_')

#: Outcomes meaning "this comparison could not reach a conclusion". They are
#: NOT successes and NOT ordinary misses — they say the evidence was unusable.
#: A reconciliation that reports success because it had nothing to compare is
#: fail-OPEN, and §10 requires this reconciliation to be fail-closed.
INCONCLUSIVE_OUTCOMES = frozenset({
    "no_listing_baseline",
    "no_rss_observations",
    "disjoint_identity_sets",
})

@dataclass(frozen=True)
class ShadowComparison:
    rss_count:int; listing_count:int; duplicate_count:int; feed_only_count:int; listing_only_count:int
    relevant_miss_count:int; rss_requests:int; listing_requests:int; request_reduction_pct:float
    normal_feeds_complete:bool; outcome:str; feed_only:tuple[str,...]; listing_only:tuple[str,...]
    relevant_misses:tuple[dict,...]
    def as_dict(self): return asdict(self)

    @property
    def is_conclusive(self) -> bool:
        return self.outcome not in INCONCLUSIVE_OUTCOMES

    @property
    def qualifies(self) -> bool:
        """True only for a clean, conclusive cycle. Anything else blocks."""
        return self.outcome == "success"

def compare_shadow(*, rss_urls: Iterable[str], listing_items: Iterable[Any], rss_requests:int, listing_requests:int, normal_feeds_complete:bool) -> ShadowComparison:
    rss={canonical_url(u) for u in rss_urls if u}
    listing={}
    for item in listing_items:
        row=_row_dict(item); url=canonical_url(row.get('url'))
        if url: listing[url]=row
    listing_urls=set(listing); duplicate=rss & listing_urls; feed_only=rss-listing_urls; listing_only=listing_urls-rss
    misses=[]
    for url in sorted(listing_only):
        row=listing[url]
        if _status_value(row) in _RELEVANT_STATES:
            misses.append({'canonical_url':url,'title':row.get('title'),'status':_status_value(row)})
    reduction=0.0
    if listing_requests>0: reduction=100.0*(listing_requests-rss_requests)/listing_requests
    outcome='success' if normal_feeds_complete else 'incomplete_feeds'
    if misses: outcome='relevant_miss'
    # ── fail-closed guards (§10) ─────────────────────────────────────────
    # Each of these previously produced 'success' — a verdict reached with no
    # usable evidence. They are checked AFTER the ordinary outcomes and override
    # them, because "we could not conclude" outranks any conclusion.
    if not listing_urls:
        # No baseline to detect misses against. Zero misses here means zero
        # comparisons were possible, not zero problems.
        outcome='no_listing_baseline'
    elif not rss:
        # RSS returned nothing at all. Without any relevant-state listing item
        # this would otherwise have scored a clean success on an empty feed.
        outcome='no_rss_observations'
    elif not duplicate:
        # Both sides have identities and NONE of them match. Two views of the
        # same source overlapping in nothing is the signature of an identity
        # mismatch, not of genuine divergence — it is exactly what ScanHound's
        # two trailing-slash-divergent canonicalisers produced when a healthy
        # 99-of-100 pipeline was reported as "0 of 100 never acquired".
        outcome='disjoint_identity_sets'
    return ShadowComparison(len(rss),len(listing_urls),len(duplicate),len(feed_only),len(listing_only),len(misses),int(rss_requests),int(listing_requests),round(reduction,2),bool(normal_feeds_complete),outcome,tuple(sorted(feed_only)),tuple(sorted(listing_only)),tuple(misses))
