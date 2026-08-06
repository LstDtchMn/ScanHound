"""Complete statistical status of the HDEncode RSS shadow qualification window.

Every figure is read live from /dbvol/crawler.db at run time. Nothing is copied
from an earlier report -- a stale number lifted from a config comment once became
a peer reviewer's headline finding, so this script exists to be re-run rather
than quoted.

Read-only.
"""
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone

con = sqlite3.connect("file:/dbvol/crawler.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row

GREEN_H, YELLOW_H = 6.0, 24.0
MIN_CYCLES, MIN_DAYS, MAX_STALE_MIN = 20, 7, 180


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def hr(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


rows = list(con.execute(
    "SELECT cycle_uuid, started_at, completed_at, outcome, normal_feeds_complete,"
    " rss_requests, listing_requests, rss_count, listing_count, duplicate_count,"
    " feed_only_count, listing_only_count, relevant_miss_count,"
    " request_reduction_pct, catchup_used, restart_recovery, details_json"
    " FROM hdencode_shadow_cycles ORDER BY completed_at"))

ELIG = ("outcome IN ('success','relevant_miss') AND normal_feeds_complete=1 "
        "AND rss_requests>0 AND listing_requests>0")


def eligible(r):
    return (r["outcome"] in ("success", "relevant_miss")
            and r["normal_feeds_complete"] == 1
            and (r["rss_requests"] or 0) > 0
            and (r["listing_requests"] or 0) > 0)


elig = [r for r in rows if eligible(r)]
inelig = [r for r in rows if not eligible(r)]

hr("1. WINDOW SIZE")
print(f"cycles recorded in total      : {len(rows)}")
print(f"cycles that COUNT (eligible)  : {len(elig)}")
print(f"cycles excluded as degraded   : {len(inelig)}")
if elig:
    f, l = ts(elig[0]["completed_at"]), ts(elig[-1]["completed_at"])
    days = (l - f).total_seconds() / 86400
    print(f"first eligible cycle          : {f.isoformat()}")
    print(f"last  eligible cycle          : {l.isoformat()}")
    print(f"observed span                 : {days:.2f} days")
    print(f"requirement                   : {MIN_CYCLES} cycles / {MIN_DAYS} days")
    print(f"                              -> cycles {len(elig)/MIN_CYCLES:.1f}x required,"
          f" duration {days/MIN_DAYS:.1f}x required")
    gaps = sorted((ts(elig[i+1]["completed_at"]) - ts(elig[i]["completed_at"])).total_seconds()/60
                  for i in range(len(elig)-1))
    print(f"cadence between eligible      : median {gaps[len(gaps)//2]:.0f} min, "
          f"min {gaps[0]:.0f}, max {gaps[-1]:.0f}")
    age_h = (datetime.now(timezone.utc) - ts(rows[-1]["completed_at"])).total_seconds()/3600
    print(f"most recent cycle of any kind : {age_h:.1f} h ago "
          f"(staleness limit {MAX_STALE_MIN} min = {MAX_STALE_MIN/60:.0f} h)")

hr("2. CYCLE OUTCOMES")
print(f"{'outcome':20s} {'feeds_ok':>8s} {'count':>6s}  {'share':>7s}")
for (o, n), k in sorted(Counter((r["outcome"], r["normal_feeds_complete"]) for r in rows).items()):
    print(f"{o:20s} {n:>8d} {k:>6d}  {100*k/len(rows):>6.1f}%")
print(f"\nzero-request breakdown:")
print(f"  rss_requests == 0      : {sum(1 for r in rows if (r['rss_requests'] or 0)==0)}")
print(f"  listing_requests == 0  : {sum(1 for r in rows if (r['listing_requests'] or 0)==0)}")
print(f"  duplicate_count == 0   : {sum(1 for r in rows if (r['duplicate_count'] or 0)==0)}")

hr("3. THE HEADLINE BENEFIT -- request reduction")
er = sum(r["rss_requests"] or 0 for r in elig)
el = sum(r["listing_requests"] or 0 for r in elig)
print(f"eligible cycles only (what the gate scores):")
print(f"  feed  requests : {er:,}")
print(f"  listing requests: {el:,}")
print(f"  reduction      : {100*(el-er)/el:.2f}%  (fewer requests to hdencode)")
ar = sum(r["rss_requests"] or 0 for r in rows)
al = sum(r["listing_requests"] or 0 for r in rows)
print(f"all cycles incl. degraded (sanity check):")
print(f"  reduction      : {100*(al-ar)/al:.2f}%")
print(f"\nabsolute saving over the window: {el-er:,} requests not made")

hr("4. MISS ACCOUNTING -- the contested number")
tot_by_col = sum(r["relevant_miss_count"] or 0 for r in rows)
tot_rows = con.execute("SELECT COUNT(*) FROM hdencode_shadow_misses").fetchone()[0]
print(f"SUM(relevant_miss_count) over ALL cycles : {tot_by_col}   <- what the app gate reads")
print(f"rows in hdencode_shadow_misses           : {tot_rows}")
print(f"SUM over ELIGIBLE cycles only            : "
      f"{sum(r['relevant_miss_count'] or 0 for r in elig)}")
print(f"SUM over degraded cycles                 : "
      f"{sum(r['relevant_miss_count'] or 0 for r in inelig)}")

cyc = {}
for r in rows:
    d = {}
    try:
        d = json.loads(r["details_json"] or "{}")
    except Exception:
        pass
    cyc[r["cycle_uuid"]] = {"r": r, "elig": eligible(r),
                            "lo": set(d.get("listing_only") or ()),
                            "fo": set(d.get("feed_only") or ())}

misses = [dict(x) for x in con.execute(
    "SELECT m.canonical_url u, m.title, m.status, m.cycle_uuid, s.completed_at at "
    "FROM hdencode_shadow_misses m JOIN hdencode_shadow_cycles s "
    "ON s.cycle_uuid=m.cycle_uuid")]
from_e = [m for m in misses if cyc[m["cycle_uuid"]]["elig"]]
from_d = [m for m in misses if not cyc[m["cycle_uuid"]]["elig"]]
print(f"\nby source cycle:  from eligible = {len(from_e)}   from degraded = {len(from_d)}")
print("degraded sources, by that cycle's feed-request count:")
for q, n in sorted(Counter(cyc[m['cycle_uuid']]['r']['rss_requests'] or 0 for m in from_d).items()):
    print(f"  feed made {q} requests -> contributed {n} 'misses'")
print("\nmiss status mix (what kind of gap was claimed):")
for s, n in Counter(m["status"] for m in misses).most_common():
    print(f"  {s:18s} {n}")

hr("5. TIERED GRADING (Jesse's rule: <=6h GREEN, 6-24h YELLOW, >24h/never RED)")
eat = [(ts(c["r"]["completed_at"]), c) for c in cyc.values() if c["elig"]]
eat.sort()


def classify(url, first):
    last_missing = first
    for at, c in eat:
        if at <= first:
            continue
        if url in c["lo"]:
            last_missing = at
        elif url in c["fo"]:
            return ("resolved", (at - first).total_seconds()/3600)
    newest = eat[-1][0] if eat else first
    h = (newest - first).total_seconds()/3600
    if last_missing > first:
        return (("red" if h > YELLOW_H else "pending"), h)
    return ("ambiguous", h)


def grade(ms, label):
    b, hours = Counter(), []
    bad = []
    for m in sorted(ms, key=lambda x: x["at"]):
        st, h = classify(m["u"], ts(m["at"]))
        tier = (("green" if h <= GREEN_H else "yellow" if h <= YELLOW_H else "red")
                if st == "resolved" else st)
        b[tier] += 1
        if st == "resolved":
            hours.append(h)
        else:
            bad.append((tier, m["at"][:19], m["u"][-58:]))
    print(f"\n{label}  (n={sum(b.values())})")
    for t in ("green", "yellow", "red", "pending", "ambiguous"):
        pct = 100*b[t]/max(1, sum(b.values()))
        print(f"  {t.upper():10s} {b[t]:4d}  {pct:5.1f}%")
    if hours:
        hs = sorted(hours)
        print(f"  catch-up latency: median {hs[len(hs)//2]:.2f} h, "
              f"min {hs[0]:.2f}, max {hs[-1]:.2f}, mean {sum(hs)/len(hs):.2f}")
        print(f"  within 1h: {sum(1 for h in hs if h<=1)}/{len(hs)}  "
              f"within 2h: {sum(1 for h in hs if h<=2)}/{len(hs)}  "
              f"within 6h: {sum(1 for h in hs if h<=6)}/{len(hs)}")
    for t, at, u in bad:
        print(f"    {t.upper()} {at} {u}")
    return b


grade(misses, "A. ALL recorded misses (the shipped gate's population)")
grade(from_e, "B. Misses from eligible cycles only (defensible population)")

hr("6. FEED HEALTH")
for r in con.execute("SELECT feed_key,last_status,consecutive_failures,"
                     "last_checked_at FROM hdencode_feed_state ORDER BY feed_key"):
    try:
        age = (datetime.now(timezone.utc) - ts(r["last_checked_at"])).total_seconds()/60
        age_s = f"{age:.0f} min ago"
    except Exception:
        age_s = "unknown"
    print(f"  {r['feed_key']:14s} http={r['last_status']} "
          f"consecutive_failures={r['consecutive_failures']} checked {age_s}")
print(f"\nrecovery cycles (restart_recovery or catchup_used) among eligible: "
      f"{sum(1 for r in elig if r['restart_recovery'] or r['catchup_used'])}")
print(f"  restart_recovery=1 : {sum(1 for r in rows if r['restart_recovery'])}")
print(f"  catchup_used=1     : {sum(1 for r in rows if r['catchup_used'])}")

hr("7. THE UNEXPLAINED 41 CYCLES")
z = [r for r in rows if (r["rss_requests"] or 0) == 0]
print(f"cycles with zero feed requests: {len(z)}")
print(f"  of those, rss_count > 0    : {sum(1 for r in z if (r['rss_count'] or 0)>0)}"
      f"   <- feed reported URLs while making no requests")
print(f"  of those, duplicate_count 0: {sum(1 for r in z if (r['duplicate_count'] or 0)==0)}"
      f"   <- feed and listing shared NOTHING")
print(f"  rss_count values seen      : "
      f"{dict(Counter(r['rss_count'] or 0 for r in z))}")
print(f"  listing_count values seen  : "
      f"{dict(sorted(Counter(r['listing_count'] or 0 for r in z).items()))}")
print(f"  they produced              : {sum(r['relevant_miss_count'] or 0 for r in z)} 'misses'")
print("\nis zero-overlap unique to them, or does it happen on eligible cycles too?")
ze = [r for r in elig if (r["duplicate_count"] or 0) == 0]
print(f"  eligible cycles with duplicate_count==0: {len(ze)} of {len(elig)}")
print(f"  eligible cycles median duplicate_count : "
      f"{sorted((r['duplicate_count'] or 0) for r in elig)[len(elig)//2]}")
print(f"  eligible cycles median rss_count       : "
      f"{sorted((r['rss_count'] or 0) for r in elig)[len(elig)//2]}")
