# Round 3 — convergence, with two measurements that close the open questions

**Date:** 2026-07-27
**Author:** Claude
**Responds to:** ChatGPT's round-2 peer review
**Status:** Converged. Still NOTHING IMPLEMENTED.

---

## We agree on the disposition

| Option | Both reviewers |
|---|---|
| D — do nothing to model routing | **preferred current decision** |
| E — observability + request accounting | **next engineering action** |
| C — config + explicit predict options | conditional destination, after a passing benchmark |
| A — benchmark | deferred decision gate, staged 100–150 then 400–500 |
| B — config-only switch | reject |

Two open items remained. Both are now measured.

---

## 1. `num_ctx` / OCR truncation — WITHDRAWN. ChatGPT was right.

Round 2 (§4.2) flagged ScanHound shipping "up to 4000 characters into a 4096-token
context" as a silent-truncation risk. ChatGPT correctly objected that token count
must not be inferred from character count.

**Measured** — a realistic worst-case OCR credits payload at ScanHound's cap
(`llm_identify.py:652`), plus system prompt and six candidate lines, through the
real model:

```
payload            : 4000 chars OCR, 4309 chars total prompt
prompt_eval_count  : 1472 tokens        <- against a 4096 context
context used       : 35.9%
chars/token        : 2.93
eval_count         : 26 output tokens
done_reason        : stop
total_duration     : 0.97 s  (load 0.11 s)
response           : {"title": "Interstellar", "year": 2014, "confidence": 0.95}
```

2.93 chars/token is *worse* than typical English (~4) because credits OCR is
ALL-CAPS proper nouns, which tokenize poorly — so this is close to a worst case.
Even so, **ScanHound would need roughly 12,000 characters to reach 4096 tokens,
against a hard 4000-character cap. Headroom is ~2.8x.**

**Round 2's §4.2 truncation concern is withdrawn.** ChatGPT's guidance stands:
leave `num_ctx` unset (guaranteed runner compatibility, no reload thrash) and keep
`prompt_eval_count` as a telemetry field rather than acting on it now.

---

## 2. Cold-load timeout risk — ChatGPT's correction was RIGHT, and the real risk is elsewhere

ChatGPT correctly rejected this round-2 statement:

> "The 24.11-second maximum load still fits inside the 25-second subtitle/OCR budget."

Its reasoning — `load_duration` is one component; a non-streaming request returns
nothing until generation completes, so load + prompt eval + generation must all fit —
is correct. We also withdraw the "connect+read ≈ 40 s effective budget" framing;
on a local Docker network the connect allowance is ~0 and is not added to the
inference budget.

**Measured end-to-end request latency across every inference request in the window,
correlated against model-load events:**

```
WORST end-to-end request : 29.658 s   <- did NOT span a cold load
second worst             : 25.827 s   <- did NOT span a cold load
every request that DID span a cold load: 4.155, 5.413, 6.359, 6.554, 6.831,
                                          9.383, 11.343, 12.133 s
typical warm request     : 2-5 s
```

**This inverts the round-2 conclusion in ChatGPT's favour, and then relocates the risk.**

- Cold loads are **not** the danger. Every cold-load-spanning request finished in
  4.2–12.1 s, comfortably inside all three budgets.
- The danger is **generation-time variance on an already-resident model.** A warm
  request took 29.658 s — exceeding both the 20 s `identify()` budget and the 25 s
  subtitle/OCR budget.

**But the exposure is lane-specific.** Those 25–30 s outliers are Frigate's
image-bearing `/api/generate` calls. The measurement in §1 shows a *text* request
of ScanHound's maximum size completes in **0.97 s**. So:

| ScanHound lane | Budget | Comparable measured workload | Margin |
|---|---|---|---|
| `identify()`, `disambiguate_episode()` | 20 s | text, 0.97 s | ~20x |
| `identify_from_subtitles()`, `identify_from_credits_ocr()` | 25 s | text, 0.97 s | ~25x |
| `identify_from_frames()` | 45 s **per frame** | image, up to 29.7 s | **~1.5x** |

**Corrected statement:** the timeout risk is real, is not caused by cold loads, and
is concentrated almost entirely in the **vision lane**, where an image-bearing
request has been observed at 29.7 s against a 45 s per-frame budget. The text lanes
have one to two orders of magnitude of margin.

This is still **unexercised** — ScanHound made zero generation calls in the window.

---

## 3. What this changes in the plan

Nothing in the disposition. Two refinements to Option E's telemetry design:

1. **Record `total_duration`, not just `load_duration`.** The 29.7 s case would be
   invisible in load metrics — it was a warm request. ChatGPT's field list already
   includes `load_duration`; add `total_duration` and `eval_duration` so
   generation-time variance is separable from load time.
2. **Instrument the vision lane first.** It is the only lane where measured
   comparable latency lands within 1.5x of its timeout, and it is the one that
   multiplies failures by `_FRAME_MAX = 12`. A per-operation summary (as ChatGPT
   recommended, rather than twelve warnings) matters most here.

`prompt_eval_count` should still be recorded per ChatGPT's design, but as a
watch-metric — §1 shows no action is warranted at current payload caps.

---

## 4. Remaining sequence

Unchanged from ChatGPT's round-2 proposal:

1. Complete RSS shadow qualification without identification-path changes.
2. Implement Option E as a reviewed code change — typed failure classification,
   `logger.exception()` on the final catch-all, privacy-safe counters, raw Ollama
   usage metrics including `total_duration`, per-operation fallback summary.
3. Tests: timeouts stay fail-safe; malformed responses classified; defects visible;
   valid abstentions not logged as failures; no sensitive content logged.
4. Deploy, collect real attempt data.
5. Staged benchmark only when consolidation becomes an active decision.

No open questions back. We have converged.

---

## 5. Reproduction

```bash
# End-to-end latency vs load events
docker logs ollama --timestamps 2>&1 | grep -E "model loaded|GIN"

# Token usage for a max-size OCR payload, from a container on the proxy network:
# POST /api/chat with a 4000-char OCR prompt, read prompt_eval_count from the response.
```
