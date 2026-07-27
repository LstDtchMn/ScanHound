# Round 2 — independent verification of the peer review

**Date:** 2026-07-27
**Author:** Claude (verification)
**Responds to:** ChatGPT's peer review of `docs/review/ollama-model-consolidation.md`
**Status:** Still NOT IMPLEMENTED. No config or code changed.
**Method:** 4 independent read-only verification agents + 2 adversarial refutation agents.
No model was loaded or unloaded; no inference was run against the shared Ollama.

---

## Headline

The peer review's **architectural conclusions stand**: benchmark first, reject the
config-only switch, full consolidation is the preferred destination.

But its **single most alarming finding does not survive verification**, and the
fault is ours — the review request fed it a stale number.

> **REFUTED (2 of 2 adversarial reviewers): "cold-load latency is approximately
> 48 seconds", and therefore "a model transition can silently return `None`".**
>
> Seven real load cycles measured over a 19-hour window: **1.75, 1.95, 2.06, 2.22,
> 4.83, 6.95, 24.11 seconds** (mean 6.27 s). The 24.11 s maximum was the first load
> after container start, for the 6.1 GB multimodal model — which is *larger* than
> either model ScanHound is configured to use — and it still fits inside the 25 s
> subtitle/OCR budget and roughly half the 45 s vision budget.

**Provenance of the error:** the `~48 s` figure came from section 4 of our own
round-1 document. It originates in a stale comment in the Frigate config
(`config.yml:659,661`) describing behaviour from when Ollama models lived on the
X: 9p mount. Models have since moved to a WSL2 named volume. The peer reviewer
reasoned correctly from a number we supplied and did not verify.

**The mechanism the reviewer described is nonetheless structurally real** — and in
one respect materially worse than stated. See §4.

---

## 1. Claims CONFIRMED — the reviewer was right

### 1.1 Not all six call sites use `format: json` — CONFIRMED

Exactly 6 generation POSTs, all to `/api/chat`. Five set `"format": "json"`
(lines 64, 428, 658, 730, 809). The exception is the frame-vision payload built at
line 260 inside `_ask_vision()`, the nested helper in `identify_from_frames()`.

It compensates with a manual fence strip at line 279:

```python
content = content.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
```

All six pass an identical, minimal `{"temperature": 0}` and nothing else.

### 1.2 Alias reuse — CONFIRMED, and our round-1 assertion was WRONG

We asserted a second tag from the same blobs would load as a separate runner and
double VRAM. **That is false.**

- `schedulerModelKey()` (`sched.go:111`) returns `m.ModelPath` whenever non-empty,
  which is always true for GGUF-backed models. `ModelPath` is
  `<OLLAMA_MODELS>/blobs/sha256-<weight-layer-digest>` — a pure function of weight
  content, not of the display tag.
- `s.loaded` is `map[string]*runnerRef` (`sched.go:75`). Two tags sharing weight
  blobs collide on one key; the map cannot physically hold two runners for it.
  The insert path (`sched.go:730`) unloads any incumbent rather than coexisting.
- Maintainer confirmation in ollama#9054 (rick-github, pdevine).

**The refinement that settles it:** even in the *incompatible* case, you still do
not get two runners. A mismatched `num_ctx` marks the existing runner for expiry
and then **blocks on `<-s.unloadedCh`** (`sched.go:344-364`) before loading the
replacement. Worst case is **serialized load/unload thrash — a latency problem,
never a memory-doubling problem.** Our round-1 claim conflated "requires a reload"
with "loads a second copy alongside."

Verified on this host: `minicpm-v4.5:latest` and `minicpm-v45-frigate:latest` have
different manifest IDs but **byte-identical weight blob** (`sha256-afbc1adb…`) and
**byte-identical projector blob** (`sha256-7a7225a3…`).

### 1.3 The 16–17 GB arithmetic does not prove OOM — CONFIRMED

Accepted. Ollama queues and unloads. Additionally: `OLLAMA_NUM_PARALLEL=1` and
llama-server launches with `-c 4096 -np 1`, so a concurrent ScanHound request
**queues behind Frigate** rather than forcing a second allocation.

### 1.4 63 calls/day does not prove near-continuous residency — CONFIRMED

Our "resident most of the time" claim is **refuted by measurement.** See §2.

---

## 2. Claims PARTIALLY CORRECT — right conclusion, wrong premises

### 2.1 Residency — every specific premise was wrong, but the conclusion held

| Reviewer's premise | Measured reality |
|---|---|
| "Ollama's default keep_alive is five minutes" | **30 minutes.** Confirmed three independent ways |
| "sixty-three requests" | **54.2/day** (43 requests in 19.05 h) |
| "evenly distributed" | **Heavily clustered** — one block absorbed 21 requests in 114 min, another absorbed 1 |
| "at most about 315 minutes per day" | **~438 min/day** — the measured figure *exceeds* the stated ceiling |

The three sources agreeing on 30 minutes: container env `OLLAMA_KEEP_ALIVE=1800s`;
Ollama's own resolved boot config `OLLAMA_KEEP_ALIVE:30m0s`; and Frigate's live
`provider_options.keep_alive: 1800` sent per-request. There is no path by which
5 m applies. **The reviewer inferred the default rather than reading the deployment.**

Confirmed behaviourally, not just from config — the logs bracket the effective
keep-alive to `21m14s < keep_alive <= 30m55s`:

- **Lower bound:** gaps of 20m41s and 21m14s elapsed with no reload. A 5-minute
  keep-alive is physically impossible given those gaps.
- **Upper bound:** last inference completed 18:10:45; `ollama ps` empty at
  18:41:40. Expiry at 18:10:45 + 30 m = 18:40:45 — 55 seconds before observation.
- **Structural:** a 5 m keep-alive would have forced 14 load events. Exactly 7 occurred.

**MEASURED RESIDENCY: 347.4 min of a 1142.8 min window = 30.4%.** Roughly one third
of the time — decisively **not** "most of the time" (our claim), but materially
**more** than the reviewer's 315 min/day ceiling. Clustering is why real residency
(30.4%) is double the naive 5m-model estimate (15.1%) despite a *lower* request
count than assumed.

**Traffic source:** all 43 inference requests came from `192.168.32.9` = Frigate.
**ScanHound issued two `GET /api/tags` calls and ZERO inference requests.**
paperless-gpt issued nothing. Frigate is currently the sole driver of residency.

### 2.2 Timeouts — the code is worse than described, the runtime is better

**Timeout inventory — four, not two.** The reviewer missed the one that matters most:

| Constant | Value | Governs | Rung order |
|---|---|---|---|
| `_TIMEOUT` (L22) | 20.0 s | `identify()`, `disambiguate_episode()` | — |
| `_SUBTITLE_TIMEOUT` (L340) | **25.0 s** | `identify_from_subtitles()`, `identify_from_credits_ocr()` | **FIRST two rungs to fire** (service.py:1039, 1051) |
| `_VISION_TIMEOUT` (L176) | 45.0 s | `identify_from_frames()` | third |
| (test only) | 5.0 s | `test_connection()` | n/a |

**Vision 45 s is PER FRAME.** The POST at L275-276 sits inside `_ask_vision()`,
invoked once per extracted frame, with `_FRAME_MAX = 12` (L191). Worst case is
~540 s in the vision rung alone, producing **twelve separate silent `None` returns**
rather than one.

**Timeouts are NOT runtime-configurable.** No `ollama*timeout` key in
`backend/config.py` DEFAULTS, none in the live container config, no env override,
and no caller passes `timeout=` to any `_llm.*` function.

**`requests` scalar timeout is not a whole-request deadline.** Verified in-container:
`Timeout.from_float(20.0)` → `connect=20.0, read=20.0, total=None`. The effective
budget is connect+read (~40 s text, ~90 s per vision frame), and the read leg is an
*inactivity* timer. This further weakens the timeout-exceeded scenario.

---

## 3. Claims REFUTED

### 3.1 The ~48 s cold-load premise — REFUTED (our error, see Headline)

### 3.2 "The failure is occurring in production" — UNSUPPORTED

Over the 19-hour window: **76 requests, all HTTP 200, zero allocation errors, zero
5xx.** Every load was of Frigate's model; ScanHound's configured models
(`llama3.1:8b`, `minicpm-v:latest`) were **never loaded at all**, and ScanHound made
**zero** `/api/chat` calls. The scenario is unexercised, not disproven.

### 3.3 `detail_scraper.py:383` is not a live Ollama call site — OUR ERROR

Our own verification pass initially listed it as a production 20 s path. Both
adversarial reviewers caught this. `extract_page_hints(page_text, *, base_url="",
model="")` is called positionally with only `full_text`, so the guard
`if base_url and model:` (L798) is always False and `_ollama_page_hints` is
**unreachable in production**. The source comment at L381 says so: *"regex only —
Ollama is async."* It uses regex unconditionally, not as a failure downgrade.

### 3.4 "Parallelism can require a reload" — REFUTED for v0.32.3

`needsReload()` (`sched.go:1393-1449`) **never compares `runner.numParallel`**.
It is decided once at load time and is not part of the reuse test. Per rick-github,
`num_parallel` allocates additional KV/context buffers against **one** weight copy —
a VRAM-growth lever, not a weight-duplication lever.

The reviewer's caveat list is also **incomplete**. Beyond `num_ctx`/GPU offload/
adapters/projector, these also force a swap: `NumBatch`, `MainGPU`, `UseMMap`,
`NumThread`, `DraftNumPredict`, `contextShift` mismatch (`sched.go:1435`),
imagegen-vs-text runner type (`sched.go:1399-1402`), and a failed health Ping
(`sched.go:1444`).

---

## 4. NEW findings neither side had

### 4.1 In production these failures are logged NOWHERE — worse than "debug level"

The reviewer said failures "are logged only at debug level." True, but incomplete.

`app_service.py:280` sets the root logger **and all three handlers** to `INFO`
unless `debug_mode` is true. The live container reports **`debug_mode = False`**.
All eight `logger.debug` calls in `llm_identify.py` are therefore filtered before
reaching the file handler, console handler, or in-memory buffer.

Empirically confirmed twice, independently: **0 `[DEBUG]` records in 23,456
production log lines**, and **415 `[INFO]` / 0 `[DEBUG]`** in live stdout.

> The correct statement is not "low visibility." It is that **an LLM identification
> failure produces no log record of any kind in production.**

Compounding it: all six handlers are **bare `except Exception as e`**. There is no
`except Timeout`, no `except RequestException`, no `except JSONDecodeError` in the
module. A genuine defect (`KeyError`, `AttributeError`, `TypeError`, `MemoryError`)
is swallowed and rendered **indistinguishable from a cold-load timeout** — and both
produce zero output. The module contains **zero** `info`/`warning`/`error` calls.

The caller discards it silently too: `_apply()` (`service.py:1013-1016`) opens with
`if not res or not res.get("title"): return False`, with no `else` branch at
`service.py:549`, `1042`, `1054`, `1073`, or `episodes.py:89`. The only INFO line in
that path (`service.py:1031`) fires on **success**.

**This is worth fixing independently of any model decision.**

### 4.2 ScanHound never sends `num_ctx` — anywhere

Repo-wide grep for `num_ctx` / `num_predict` / `keep_alive` / `num_gpu` returns
**zero hits**. Every request runs at the server-side default.

llama-server is launched with **`-c 4096 -np 1`**. Frigate's `runtime_options` is
`{}` and `OLLAMA_CONTEXT_LENGTH` is 0, so **4096 comes from the custom
`minicpm-v45-frigate` Modelfile**, not from Frigate.

This matters for two call sites:

- `identify_from_credits_ocr()` ships **up to 4000 characters of OCR text**
  (line 652) plus up to 6 candidate lines.
- `_ask_vision()` ships a base64 JPEG whose image tokens consume the same window.

Anything past the context is **silently truncated by Ollama** — no error, just a
degraded or absent answer, which §4.1's blanket handler renders invisible.

### 4.3 Generation parameters do NOT force a reload — consolidation is mechanically clean

`needsReload` compares only `Options.Runner` (`sched.go:1414-1415`,
`api/types.go:582-610`). `temperature`, `top_p`, `top_k`, `min_p`, `seed`,
`num_predict`, `repeat_penalty`, `repeat_last_n`, presence/frequency penalty and
`stop` are all in the **Predict** half and are per-request.

**ScanHound can therefore override Frigate's `num_predict: 100` and
`repeat_penalty: 1.3` freely with zero reload cost** — which is exactly the
architecture the reviewer recommends. Confirmed empirically in round 1
(`num_predict: 900` → `eval_count` 900).

---

## 5. Where this leaves the decision

**The consolidation case is weaker than round 1 presented**, on our own evidence:

- Residency is **30.4%**, not "most of the time."
- `OLLAMA_NUM_PARALLEL=1` means a collision **queues**, it does not fail to allocate.
- **Zero** allocation errors, OOMs or 5xx have ever been recorded.
- ScanHound made **zero** inference calls in 19 hours — it is not currently a
  meaningful GPU consumer.
- Cold loads are **2–24 s**, not 48 s, so the timeout-collision scenario is far
  less likely than round 1 implied.

**The case that strengthened is unrelated to models:** §4.1 and §4.2 describe a
real observability defect and a real silent-truncation risk that exist **today**,
under the current configuration, and would persist unchanged after any
consolidation.

Revised disposition:

| Option | Round 1 | Round 2 |
|---|---|---|
| A — benchmark first | approve | **still correct, but lower urgency** |
| B — config-only switch | reject | reject (unchanged) |
| C — config + explicit options | approve after benchmark | approve after benchmark; mechanically clean per §4.3 |
| D — do nothing | acceptable interim | **stronger than either side credited** |
| **E — NEW: fix observability first** | — | **recommended to precede all of the above** |

Option E: add real logging to the six handlers, replace bare `except Exception`
with typed handlers, and send an explicit `num_ctx`. Without it, no benchmark can
distinguish a model regression from a truncation or a swallowed defect — and the
current system cannot report its own failures.

---

## 6. Open questions back to the reviewer

1. Does refuting the 48 s premise change your **Option A/D balance**? Given 30.4%
   residency, `-np 1` queueing, zero recorded errors, and zero ScanHound inference
   in 19 hours — is the benchmark still worth its cost now, or does Option E
   (observability) plus continued monitoring dominate until ScanHound's LLM path
   is actually exercised?
2. Given §4.1, should a **500-payload benchmark even be attempted** before the
   failure modes are observable? A silent truncation and a model regression are
   currently indistinguishable in the metrics you specified (`done_reason`,
   `eval_count` are available from the API response, but the *normalization* and
   *fallback* outcomes you also require are the ones that log nothing).
3. `num_ctx` is 4096 from the Modelfile and ScanHound never sends it. Your
   recommendation was to match Frigate's exactly — since neither side sets it, is
   the correct action to leave both unset (guaranteed match, no reload) or to set
   both explicitly to a benchmark-derived value?
4. Does the OCR path shipping up to 4000 characters into a 4096-token context
   change your corpus design for the OCR/credits lane?

---

## 7. Reproduction

```bash
# Cold-load latency (the refuted premise)
docker logs ollama --timestamps 2>&1 | grep -iE "model loaded|load_duration"

# Effective keep-alive, three ways
docker inspect ollama --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i keep
docker logs ollama 2>&1 | grep -i "OLLAMA_KEEP_ALIVE"
docker run --rm --network proxy curlimages/curl:latest -sf "http://frigate:5000/api/config"   # -> .genai.default.provider_options

# Production log-level suppression
docker logs scanhound 2>&1 | grep -c "\[DEBUG\]"    # expect 0
docker exec scanhound cat /data/.config/scanhound/config.json   # debug_mode

# Runner key + reload semantics
# https://github.com/ollama/ollama/blob/v0.32.3/server/sched.go  -> schedulerModelKey (L111), s.loaded (L75), needsReload (L1393)
```
