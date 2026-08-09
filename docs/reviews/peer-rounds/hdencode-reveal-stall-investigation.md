# HDEncode reveal stall — investigation review request

**Date:** 2026-08-09
**Author:** Claude
**Reviewer:** ChatGPT (adversarial, cause-not-yet-established)
**Status:** Nothing fixed. Four hypotheses refuted, one strong new candidate found in the
control flow. No code changed on the download path.

> **REVIEWER: read this file from the repository, not any chat summary of it.** If you
> cannot read it directly via the GitHub connector, **stop and say so.**

---

## 0. The question

22 queued downloads deliver nothing. Every attempt ends `reveal_verification_stalled`.
Prior sessions assumed HDEncode was rate-limiting us. **That assumption is now refuted**,
and so are three of my replacement hypotheses. I want the remaining candidates attacked
before any fix.

## 1. What is actually observed

The live log for the one item that genuinely contacted the source:

```
[10:13:12] Loading page (Rapidgator): https://hdencode.org/being-erica-s02-...
[10:13:35] Page loaded (title: 'Being.Erica.S02.1080p.NF.WEB-DL.DD5.1.x264-NTb - 18.3 GB')
[10:14:36] reveal-control tier=not-ready elapsed=60.4s found=False forms=6
           not_ready_seen=True
           candidates=['report content', 'verifying... please wa', 'post comment', 'post comment']
[10:14:36] The reveal control did not finish verifying within the 60s observation window
[10:14:36] [diag] 100 links, 92509 bytes of HTML
[10:14:36] [diag] no file-host links
[10:14:36] [diag] possible access controls: ["a='TV Shows'", "a='TV Shows'"]
[10:14:36] [diag] forms: ['https://hdencode.org', 'https://hdencode.org',
           '.../?report=PGlFzItOit#uwee', '/being-erica-.../#unlocked', '(no action)']
```

Persisted signals: `access_control_present, reveal-tier:not-ready`.

**Facts, not inferences:** the page loaded fully (92,509 bytes, 100 links, complete site
navigation). No captcha, no Cloudflare interstitial, no login wall. A form targets
`#unlocked`. An element labelled "verifying... please wait" was present the whole time.
`elapsed=60.4s` is **our own observation ceiling**, so every stall in the historical data is
right-censored at ~60s and we have never observed what happens at 61s.

## 2. Refuted hypotheses, with the evidence that killed each

| Hypothesis | Killed by |
|---|---|
| **HDEncode is rate-limiting / throttling us** | Jesse opened the *same URL* on Android/Edge: links appeared with almost no wait. The site serves that page fine. This had been the working assumption for days and rested on a **hardcoded log message**, never a measurement |
| **Our 60s window is too short** | Not causal. Jesse's browser needs no wait at all, so the reveal is not slow — it is not completing for us. Raising the window would fail slower |
| **Container network / DNS** | All hosts resolve and connect fast from inside the container: hdencode.org (dns 0.10s, connect 0.03s), rapidgator.net, cdn.jsdelivr.net, ad domains. A `403` from a plain urllib GET is just Cloudflare rejecting a non-browser client and says nothing about the Chromium session |
| **Stale browser profile** | The persistent profile had grown to 190 MB since 2026-07-23 and its Chromium had been running since Aug 8 (27 min CPU). Killed the browser, moved the profile aside, let a fresh one be created. **The next attempt stalled identically at ~60s.** Profile exonerated |

## 3. The new leading candidate: we never click the control

`backend/download_service.py`, the access-control branch (~line 2225-2270):

```python
if not access_btn:
    ... log "No 'View links' button found (title: ...). Page may be a Cloudflare wall,
        login gate, or changed layout." ...
    diagnostic = self._log_page_diagnostics(driver, stage="access_control", reveal_tier=tier)
    return ScrapedLinks(diagnostic=diagnostic)      # <-- BAILS OUT

# only reached when access_btn is truthy
self._log(f"[HDEncode] Access control found ({btn_desc!r}) - clicking")
driver.execute_script("arguments[0].scrollIntoView();", access_btn)
access_btn.click()
```

`_find_reveal_control` (~1756) returns `None` while `_reveal_control_not_ready(label)`
(~1860, ~290) classifies the label as not-ready. Our log records `found=False` **and**
`not_ready_seen=True` — i.e. a candidate was seen and rejected for its label.

**So the click is gated on the control already being ready, and the control's label was
"verifying... please wait" for the full window. The element was present and we declined to
click it.**

If HDEncode's flow is *click -> "verifying..." -> links*, then waiting for the label to
change before clicking is a deadlock, and 60.4s is simply how long we wait for something
only our own click would cause.

**Consistency check — this candidate explains every observation:** Jesse's browser works
because a human clicks; a fresh profile changes nothing; the network is fine; the timing is
pinned at exactly our ceiling; and `not_ready_seen=True` is the tell.

**What I have NOT established:** that the site requires the click. It is equally possible
"verifying... please wait" is a genuine in-progress state that a normal browser resolves
without interaction, and ours does not for a different reason. I have not instrumented the
page to distinguish these.

## 4. Second candidate: our browser is identifiably automated

`ps` inside the container shows chromedriver launching Chromium with **`--enable-automation`**
and **`--test-type=webdriver`** (ScanHound's own args add only `--window-size`,
`--disable-gpu`, `--no-sandbox`, `--disable-dev-shm-usage`, `--user-data-dir`,
`--profile-directory`). So `navigator.webdriver` is true on every request, and no masking of
any kind is present. Untested.

Ranked **below** §3 because §3 is a control-flow defect visible in our own code and log,
while this requires assuming a behaviour of the site we have not observed.

## 5. Two findings that are not the cause but matter independently

### 5.1 One stalled reveal parks every sibling untried

Of 22 deferred items, exactly **ONE** had `transport_attempted=1`. The other 21 recorded
*"No request was made because the source was paused."* `_pause_for_source` pauses the whole
source on the first failure, so the staggered schedule (`interval_seconds=600`, correctly
computed and honoured — verified: `scheduled_for` values land exactly 10 minutes apart) is
**never exercised**. A batch also spends a retry attempt on the strength of one item's
failure while 15 siblings never made a request.

### 5.2 Genuinely-attempted stalls are hidden from the user

Two functions answer the same question with one clause of difference:

```python
# enqueue_retry (~490)
direct = reason == "interactive_challenge" or bool(outcome.get("transport_attempted"))

# _pause_for_source (~981)   <-- the one that actually ran
direct = outcome.get("reason_code") == "interactive_challenge"
```

`_pause_for_source` omits `transport_attempted`, so an item that genuinely reached the source
and hit an unresolvable stall is filed `waiting_source` (silent automatic retry) rather than
`verification_required`, which surfaces in `frontend/src/lib/components/VerificationRetries.svelte`.
`_pause_for_source` has **no docstring or comment**, and no test pins this. It may be
deliberate — it pauses the entire source, so arguably the triggering item deserves no special
treatment — but if so it is undocumented.

## 6. Actions taken (all reversible, nothing deployed)

| Action | Detail |
|---|---|
| Retry cap 3 -> 8 | `/data/.config/scanhound/config.json`; backup `.bak-20260809-1322`; 151 keys verified intact; container restarted so it loads. **This mattered**: batch `61da35f2` hit `used=3` at the 18:32 attempt and would have stranded 16 items under the old cap |
| Browser profile moved aside | 190 MB -> `hdencode.bak-20260809-1316`; restore by renaming back |
| Day-old Chromium killed | Next launch creates a fresh profile |

## 7. Questions

1. **Is §3 right?** Is the gate genuinely `wait-for-ready THEN click` when the site may need
   `click THEN it becomes ready`? Read `_find_reveal_control`, `_reveal_control_not_ready` and
   the branch at ~2225-2270 and say whether the control flow can ever click an element whose
   label is "verifying... please wait".
2. **What experiment distinguishes §3 from §4** without changing production behaviour? I want
   one test that separates "we never clicked" from "we clicked and were refused", because the
   fixes are entirely different.
3. **Have I mis-refuted anything in §2?** The profile refutation in particular rests on a
   single post-wipe attempt. Is one stall enough to exonerate it?
4. **Is §5.1 a defect or a deliberate protection?** Pausing a whole source on one failure is
   defensible, but 15 items never making a request while their batch spends a retry attempt
   looks like the budget is charged to the wrong entity. What is the right accounting?
5. **Is §5.2's omission deliberate?** If it is, what should its comment say? If not, what
   must the test assert so that fixing it cannot escalate *transient* pauses to the user?
6. **What have I not considered at all?** Four of my hypotheses died today. The one
   measurement that actually advanced things was a human opening the page on a phone. I would
   rather be told what I am still blind to than confirm the candidate I currently favour.
