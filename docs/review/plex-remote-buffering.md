# Review request: Plex remote-stream buffering

**Date:** 2026-07-28
**Author:** Claude (investigation)
**For:** ChatGPT peer review
**Decision owner:** Jesse
**Status:** NOTHING CHANGED. No Plex setting modified, no service restarted, no database touched.
All work was read-only. Seeking review before implementing.

> Viewer names, account names and IP addresses are redacted for this public repo.
> Devices are identified by role. No tokens or API keys appear anywhere in this document.

---

## 1. The question

Tautulli has been emailing buffer warnings for remote Plex streams. Why are remote
streams buffering, and are the notifications trustworthy?

**Server:** native Windows Plex 1.43.3.10828. Transcodes on an Intel UHD 770 iGPU
(`HardwareDevicePath` pinned to Intel). An RTX 5070 also exists in the box but is
held by an Ollama container. 2.5 Gbps LAN. Residential fibre.

---

## 2. Are the notifications real? YES — and they under-report

Over 35 days (2026-06-23 → 2026-07-27):

| | |
|---|---|
| Raw stall moments Plex reported to Tautulli | **895** across 222 sessions |
| Emails Tautulli actually sent | **23** |
| Suppression rate | **97.4%** |
| Threshold crossings that produced an email | **17 of 17** |
| Emails below threshold (false alarms) | **0** |

`buffer_threshold = 10` is the stock value **and a hard floor** — Tautulli's
`config.py` rewrites any lower value back to 10, so it cannot be made more
sensitive. `buffer_wait = 900` is also stock. Nobody dialled this up.

**All 23 emails were `location = wan`. Zero from the LAN.** The local set-top box has
853 sessions all-time and has never once crossed the threshold.

One genuine design wart, low impact: after a session's first warning, the
"re-send after 15 min" path (`activity_handler.py:221-222`) does not re-check the
count-of-10, so a single later stall re-emails. That produced 6 of the 23. Worst case
was 4 emails for one 111-minute film — which had genuinely stalled 20 times.

**Conclusion: the alerting is accurate. Do not raise the threshold.**

### Methodology note that matters for anyone reproducing this

`session_history` has **no `buffer_count` column** — buffering lives only in the live
`sessions` table and is discarded when a session ends. It survives in exactly two
places: `notify_log` `on_buffer` rows (threshold-gated, so severe only), and
`tautulli.log` DEBUG lines `"Session N buffer count is M"` (every event, requires
`verbose_logs = 1`). **Querying only `notify_log` massively undercounts** and would
falsely suggest the LAN never buffers.

---

## 3. Primary finding: remote clients are handed streams their link cannot carry

**There is no per-stream remote cap.** `WanPerStreamMaxUploadRate = 0`,
`WanTotalMaxUploadRate = 0`, `WanPerUserStreamCount = 0` — all unlimited. Plex serves
whatever the client requests, and at least one remote client is set to
*Original / Maximum*.

Buffering probability rises monotonically with stream bitrate, on WAN:

| Remote stream bitrate | Sessions that buffered |
|---|---|
| < 5 Mbps | 33.3% |
| 5–15 Mbps | 40.6% |
| 15–40 Mbps | 56.8% |
| **40+ Mbps** | **90.0%** |

The 40+ Mbps tier is only 10 sessions but produced **164 of 627 WAN buffer events (26%)**.

Worst single session on record: a remote streaming stick **direct-playing 4K at
93,109 kbps**, producing **65 stall events**. Seven remote sessions landed in the
80–200 Mbps band. All are 4K Dolby Vision / HDR10 titles.

**Severity split (30 days, 544 matched sessions):**

| | LAN (178) | WAN (366) |
|---|---|---|
| no buffering | 80.3% | 64.8% |
| trivial (1–2) | 16.9% | 19.4% |
| moderate (3–9) | 2.8% | 11.7% |
| **severe (10+)** | **0.0%** | **4.1%** |

Worst LAN session ever: 8 stalls. Worst WAN session: 65.

### The server is NOT the constraint

- **Measured upload: 483 Mbps.** Busiest hour in a month averaged **90.5 Mbps (~19%)**.
- 83.2% of all remote-streaming time is under 25 Mbps.
- Peak concurrency in 30 days: **5 streams / 3 transcodes**, against limits of 6 iGPU + 2 CPU.
- At buffer moments, concurrency was 1–3 streams; **12 of 21 notification events had
  exactly ONE WAN stream active**.

### Ruled out, with evidence

| Hypothesis | Verdict |
|---|---|
| **Plex Relay** (capped ~1–2 Mbps, always buffers) | **`relayed = 0` on all 3,100 sessions ever recorded.** Never used once. |
| NIC / network faults | 0 errors, 0 discards, 0.048% TCP retransmit, 2.5 Gbps link, no QoS |
| VPN in the path | VPN adapter Disconnected; single default route on the physical NIC |
| Storage | Transcode temp on NVMe, 0.1–0.4 ms latency, 319 GB free. **No Plex library path touches the degraded array at all.** |
| Server upstream saturation | 483 Mbps measured vs 90.5 Mbps peak hour |

**Interpretation: this is a last-mile/client-side ceiling, not a server one — but it
is addressable server-side by capping per-stream remote bitrate.**

---

## 4. Secondary finding: database lock contention stalls playback writes

Independent of bandwidth, and it affects LAN playback too.

**Lock holders** (928 "held transaction too long" events, ~1,540 s instrumented):

| Holder | Count | Max | Total lock-hold |
|---|---|---|---|
| `MetadataCollection.cpp:522` | 332 | 5.94 s | 701 s |
| `MetadataCollection.cpp:1130` | 218 | 6.95 s | 484 s |
| `FullTextSearch.cpp:81` | 3 | **64.16 s** | 186 s |
| `LibrarySectionPutAll.cpp:874` | 21 | 4.64 s | 81 s |

**Collection machinery = 1,266 s of ~1,540 s (82%).**

**Victims** (233 "took too long to start a transaction"):

| Victim | Count | Max wait |
|---|---|---|
| `MediaSubscriptionDesiredSetManager.cpp:105` (DVR/Live TV) | 130 | 5.84 s |
| **`MetadataItemSetting.cpp:409` (playback progress / watched state)** | **42** | **14.12 s** |
| `MediaGrab.cpp:35` (Live TV grabber) | 1 | 6.58 s |

The Live TV grabber is a **victim, not a holder** — worth stating because it is easy
to misread as the cause.

**Cause: a scheduling collision.** Kometa (Docker, `KOMETA_TIME=04:00`) runs daily —
5 h 12 m yesterday, 6.4 h and still running today at item 4,724 of 11,193. It
therefore overlaps Plex's **07:00 scheduled library scan every single day**, which is
exactly where the recurring 07:00 slow-query bump sits.

### Two of our own figures were wrong — corrected here

1. **"13,050 slow queries in 4 days" is not a steady state.** 7,346 of them (56%)
   occurred in ONE hour: 2026-07-24 19:00.
2. **"29 + 24 busy-database errors" is two stall events, not 53 incidents** —
   07-24 19:08:19–19:08:39 and 07-28 07:00:20–07:04:23.
3. The 07-24 19:00 storm was **self-inflicted**: Windows Event 1074 shows a shutdown
   initiated from the Start menu at 19:09:42, mid-peak-viewing. Plex returned at
   19:20:47 and immediately ran startup `DatabaseFixups` (16.7 s, 9.28 s holds), a
   full-text-search index rebuild (**64.16 s hold**) and the 6-hourly scan of all four
   sections. **No Plex crash occurred** — no dumps, no WER entries.

### The WAL is NOT a leak — corrected

Reported as "WAL 1,334 MB vs 1,328 MB DB, stuck checkpoint". **That reading was wrong.**

Both WALs are ~100% of their own database size (main 339,503 frames vs 340,058 pages
= 99.8%; blobs 611,920 vs 611,537 = 100.1%). That is the fingerprint of a **single
whole-database rewrite**, not gradual growth. Both `-wal`/`-shm` files were created
**2026-07-16 22:08:13** — the same minute as the Plex 1.43.3 upgrade launcher log. The
upgrade's schema migration rewrote every page and stretched the files; SQLite never
shrinks a WAL, so they sit at that high-water mark.

**Checkpointing verifiably still works** — the main `.db` mtime advanced during
observation, and in WAL mode only a checkpoint writes that file. Net effect: ~3.7 GB
of stranded disk, not a fault. (The blobs WAL at 2,404 MiB is larger than the main
one and was missing from the original report.)

### ⚠️ Hazard found

**The Windows registry disagrees with the running Plex server.** A restart — including
a routine one — would silently apply the registry's values. This must be reconciled
before any restart.

Also: `ButlerEndHour = 3` but the credits scanner was still running at 03:02, i.e. the
maintenance window is too short for the work scheduled inside it.

---

## 5. A figure we published and then had to retract

We initially reported **"20% of transcodes run with no hardware acceleration"** (202
of 1,008). **That was an artifact of the counting.** Corrected:

- **148 are audio-only** conversions (`video_decision = copy`; only the soundtrack is
  converted, typically E-AC3 → Opus for streaming sticks). No video encoder runs, so
  "no hardware" is the correct record.
- **16 are container repackaging** — Plex did not even request hardware
  (`transcode_hw_requested = 0`).
- **844 are real video conversions. 806 used hardware; 38 fell back.**

**True software-fallback rate: 38/844 = 4.5%**, and **2.9% whenever Plex is correctly
bound to the Intel iGPU.**

Likewise, 485 sessions flagged "no hardware decode" are **all MPEG-2 Live TV** — the
UHD 770 has no usable MPEG-2 decoder in this Plex build, so software decode plus
hardware encode is intended behaviour, not a failure.

### What actually causes the 38 real fallbacks

Not content. Every content hypothesis was tested and refuted (Dolby Vision, HDR10,
tone-mapping, 10-bit, interlacing, MPEG-2, VC-1, resolution, subtitles, concurrency) —
each type appears in far larger numbers in the *hardware* group.

**The split is by which GPU Plex was bound to:**

| Bound device | Software fallback rate |
|---|---|
| Intel iGPU (intended) | **2.9%** |
| RTX 5070 (drifted) | **15.7%** |
| — MPEG-2 Live TV subset | **1.6% vs 21.4%** (13×, χ² = 56.0, 1 df, p ≪ 0.001) |

**Mechanism:** the Intel pin was lost across two crashes on 2026-07-17/18; Plex
auto-selected the RTX 5070 and stayed there until it was manually re-pinned on
2026-07-24 19:27. The 5070 is held by an Ollama container, and Plex logged **NVIDIA
encoder out-of-memory errors** in that window — the second-stage fall from NVENC to
pure software.

**This is already fixed** (re-pinned 07-24). Its value now is as a *leading indicator*:
any `transcode_hw_encode = nvenc` row is proof the Intel pin has been lost again.

---

## 6. Unexplained — flagged rather than hand-waved

1. **Friday outlier.** 2.71 buffer events per session on Fridays vs 0.61–1.03 every
   other day; **38.6% of all events from 16.4% of sessions.** No explanation from
   Tautulli data alone.
2. **Evening skew.** 19:00–23:59 is 26.5% of sessions but **46.9% of buffer events**
   (21:00 worst: 52.9% of sessions buffered). Partly explained by the 07-24 restart,
   but not fully.
3. **21 software fallbacks during iGPU-bound periods** remain unexplained, because
   `TranscoderLogLevel = error` and `TranscoderSessionLogLevel = quiet` — Plex is not
   logging *why* it selects or rejects a device. The `TranscodingSessionLogs` folder
   is empty.
4. **LAN 4K direct-play at 40+ Mbps buffered in 20 of 66 sessions (30.3%)** while the
   15–40 Mbps LAN band buffered 0 of 12. Low confidence — all LAN cases are ≤8 stalls
   and n=12 in the comparison band.
5. **A remote set-top box is pinned to exactly 1,720 kbps** (min = max across 100
   sessions), forcing a 96% transcode rate and SD output. This is a **client-side**
   remote-quality setting. It explains poor picture, **not** buffering — 1.7 Mbps over
   a 483 Mbps uplink cannot starve.

---

## 7. Proposed actions — NOT YET IMPLEMENTED

| # | Action | Rationale | Risk |
|---|---|---|---|
| 1 | Set `WanPerStreamMaxUploadRate` ≈ 20,000–25,000 kbps | Stops a single remote stick being handed a 93 Mbps remux it cannot pull. 83% of remote viewing is already <25 Mbps, so most users unaffected | Caps quality for the one viewer deliberately requesting Original |
| 2 | Move Kometa off 04:00, or move Plex's scheduled scan, so they stop overlapping | Removes the daily 07:00 collision; collection machinery is 82% of lock-hold time | Kometa runs longer/later; needs a window that avoids peak viewing |
| 3 | Reconcile the registry with the running Plex config **before any restart** | A restart would silently apply stale registry values | Doing nothing leaves a landmine on the next reboot |
| 4 | Raise `TranscoderLogLevel` to debug temporarily | The only way to explain the 21 iGPU-era fallbacks; Plex currently logs nothing about device selection | Larger logs; no behaviour change |
| 5 | Alert on any `transcode_hw_encode = nvenc` | Proven leading indicator that the Intel pin was lost; preceded both incidents | None — read-only monitoring |
| 6 | Ask the heaviest remote viewer to set a fixed remote quality in their app | Fixes it at source; #1 enforces it server-side regardless | Social, not technical |
| 7 | Extend `ButlerEndHour` past 03:00 | Credits scanner still running at 03:02, outside its window | Maintenance runs later into the morning |

---

## 8. Questions for review

1. **Is 20–25 Mbps the right per-stream cap**, or should it be lower given that the
   40+ Mbps tier buffers 90% of the time and the 15–40 tier still buffers 57%? Is
   there an argument for ~10 Mbps, or does that punish good connections
   unnecessarily?
2. **Is capping server-side the right lever at all**, versus fixing client-side
   remote-quality settings? The server can demonstrably source the bitrate; the
   clients cannot receive it. Does a server cap risk masking a genuinely different
   problem for a viewer whose link *could* carry more?
3. **The Kometa/Plex collision** — is rescheduling sufficient, or does a 5–6 hour
   daily collection run against a 1.3 GB library database indicate something else
   wrong (over-broad collection definitions, unnecessary re-tagging)? 82% of lock-hold
   time from collection machinery seems high.
4. **The Friday outlier** — any hypothesis worth testing? It is the largest unexplained
   signal in the dataset (38.6% of events from 16.4% of sessions).
5. **Is the ~3.7 GB of stranded WAL worth reclaiming**, given checkpointing works and
   the only cost is disk (C: at 17.1% free)? Reclaiming requires stopping Plex, which
   is currently hazardous per #3.
6. **Have we mis-framed anything else?** Two headline figures in this investigation
   (the 20% fallback and the WAL "leak") turned out to be counting artifacts. We would
   rather find a third now than after implementing.

---

## 9. Reproduction

```bash
# Buffering is NOT in session_history. Two sources only:
#   notify_log on_buffer rows (threshold-gated)
#   tautulli.log DEBUG "Session N buffer count is M"  (needs verbose_logs = 1)
docker exec tautulli sqlite3 "file:/config/tautulli.db?mode=ro" \
  "SELECT notify_action, COUNT(*) FROM notify_log GROUP BY notify_action;"

# Relay check (all time)
docker exec tautulli sqlite3 "file:/config/tautulli.db?mode=ro" \
  "SELECT relayed, COUNT(*) FROM session_history GROUP BY relayed;"

# Plex settings - read via the API, NOT the registry.
# An absent registry key means "using the default", which has caused
# at least one wrong conclusion on this host.
#   GET http://localhost:32400/:/prefs?X-Plex-Token=<redacted>

# Lock holders vs victims
#   "Held transaction for too long"        -> names the HOLDER
#   "Took too long to start a transaction" -> names the VICTIM
```
