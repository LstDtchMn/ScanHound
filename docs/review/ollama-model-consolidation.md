# Review request: consolidate ScanHound onto the shared Frigate Ollama model?

**Date:** 2026-07-27
**Author:** Claude (analysis + measurements)
**For:** ChatGPT peer review
**Decision owner:** Jesse
**Status:** NOT IMPLEMENTED — no config or code changed. Seeking a second opinion first.

> Note: all measurements below are from the live host. Personal data (licence plate
> strings, recognised person names) has been redacted for this public repo.

---

## 1. The question

ScanHound currently pulls two of its own Ollama models. Frigate pulls a third.
Should ScanHound be repointed at the model Frigate already keeps resident?

| Model | Size | Consumer | Purpose |
|---|---|---|---|
| `llama3.1:8b` | 4.9 GB | ScanHound | text — filename/metadata identification |
| `minicpm-v:latest` | 5.5 GB | ScanHound | vision — video frame identification |
| `minicpm-v45-frigate` | 6.1 GB | Frigate | vision + text — camera descriptions |

All three live in one shared `ollama` container (v0.32.3), which is defined inside
the Paperless compose stack and reached over the `proxy` Docker network at
`http://ollama:11434`. Port 11434 is **not** published to the host.

---

## 2. Why this became urgent today

Frigate's `review.genai` feature was enabled on 2026-07-27. It generates an
AI summary + threat-level rating for every alert (~63 alerts/day after separate
alert-tuning work the same day).

Previously Frigate's GenAI calls were sparse — only 7 descriptions across 300
events, because descriptions are scoped to property zones and ~66% of events are
street traffic that is deliberately excluded. The model therefore unloaded to idle
between bursts.

With `review.genai` on, calls now fire on every alert, so **the 6.1 GB model stays
resident far more of the time**. The window in which ScanHound could safely load
its own model has narrowed.

---

## 3. Measured VRAM facts (RTX 5070, 12,227 MiB total)

| State | Used | Free |
|---|---|---|
| Frigate running, Ollama idle (measured 2026-07-27) | 3,437 MiB | 8,507 MiB |
| Frigate running, Ollama idle (measured 2026-07-25) | 4,401 MiB | 7,543 MiB |
| Frigate + one vision model resident (measured 2026-07-25) | 11,237 MiB | 707 MiB (**94%**) |

**A resident vision model costs ~6,836 MiB (~6.7 GB), not the 5.4–6.1 GB that
`ollama list` / `/api/ps` report** — the delta is CUDA context overhead. Capacity
math should use the measured delta.

**Saturation does not degrade already-resident work.** Detector inference measured
6.21/8.68/5.96 ms at 94% VRAM vs 9.01/6.09/4.94 ms with Ollama unloaded —
statistically indistinguishable. The exposure is **allocation failure for new
work**, not slowdown.

**Worst case today:** Frigate floor (~3.4–4.4 GB) + Frigate GenAI model (~6.7 GB)
+ ScanHound model (~6 GB) ≈ **16–17 GB against a 12 GB card.**

Current GPU state at time of writing: 7% utilisation, 47 °C, no errors in Ollama's
log history, no OOM events, no failed unloads.

---

## 4. What consolidation would buy

1. **Eliminates the collision entirely** — one resident model serves both consumers,
   so the 16–17 GB worst case cannot occur.
2. **Frees ~10.4 GB nominal disk** (`llama3.1:8b` + `minicpm-v`). Caveat: actual
   savings are lower where models share base blobs. `minicpm-v4.5` and
   `minicpm-v45-frigate` are known to share blobs (deleting one frees ≈nothing);
   whether `minicpm-v:latest` shares with them is **unverified** and should be
   checked before claiming the figure.
3. **Removes ScanHound's cold-load latency** (~48 s) whenever Frigate has the model
   warm — which, post-`review.genai`, is most of the time.

---

## 5. The technical catch

`minicpm-v45-frigate` is a **custom Modelfile tuned for Frigate**:

```
PARAMETER num_predict 100
PARAMETER repeat_penalty 1.3
PARAMETER temperature 0.2
PARAMETER stop <|im_start|>
PARAMETER stop <|im_end|>
```

`num_predict 100` caps output at 100 tokens — correct for short camera captions,
potentially wrong for ScanHound's JSON responses. `repeat_penalty 1.3` is also
unusually high for structured JSON output, which is inherently repetitive.

ScanHound calls `/api/chat` at 6 sites in `backend/rename/llm_identify.py`
(lines ~73, 275, 436, 666, 743, 818). Every call sets:

```python
"format": "json", "stream": False, "options": {"temperature": 0}
```

It overrides `temperature` but **not** `num_predict` or `repeat_penalty`, so it
would inherit Frigate's values.

The model's TEMPLATE does contain a `{{- if .Messages }}` branch, so `/api/chat`
is supported — the template is not a blocker.

---

## 6. What was empirically verified (not assumed)

**Test A — does a request-level `num_predict` override the Modelfile parameter?**
Same prompt, one call without `num_predict`, one with `num_predict: 900`.

| Call | `eval_count` |
|---|---|
| no override | **100** (hit the Modelfile cap) |
| `num_predict: 900` | **900** |

**Result: YES, request options override Modelfile parameters.** The cap is not a
hard barrier — ScanHound can lift it per request.

**Test B — does the Frigate model handle a realistic ScanHound task?**
Prompt: identify `The.Matrix.1999.2160p.UHD.BluRay.x265-TERMINAL.mkv`, respond
only with JSON containing title/year/confidence.

```json
{"title": "The Matrix", "year": 1999, "confidence": 1.0}
```

25 tokens. Valid JSON. Correct. **Well within the 100-token cap even unmodified.**

**Caveat on Test B:** this is a single, easy case — a famous film with a clean
scene-release filename. It is evidence that the plumbing works, **not** evidence
of accuracy parity across ScanHound's real corpus.

---

## 7. The genuine unknown

`llama3.1:8b` (text-specialised, 6 months old) vs `minicpm-v4.5` (multimodal) on
**text-only filename identification**. One successful test case says nothing about
accuracy across ambiguous releases, foreign titles, anime, multi-episode packs, or
year-collision cases.

The vision swap (`minicpm-v` → `minicpm-v4.5`) is *probably* an upgrade on version
grounds alone, but that is also unverified.

---

## 8. Options as they stand

| # | Option | Cost | Risk |
|---|---|---|---|
| A | Benchmark both models on the real corpus first, then decide | time only | none — nothing changes |
| B | Config-only switch: repoint the two settings, accept the 100-token cap | minutes, reversible in seconds | silent truncation on long answers; unmeasured accuracy delta |
| C | Config + code: also add `num_predict`/`repeat_penalty` to the 6 call sites | PR + review + test | same accuracy unknown, but no truncation |
| D | Do nothing | none | 16–17 GB worst case remains possible |

**Timing constraint:** an RSS shadow-qualification window closes ~2026-07-28.
Changing ScanHound's identification model before it closes would contaminate that
measurement. Options B and C should wait until it does.

---

## 9. Specific questions for ChatGPT

1. **Is the single-model consolidation architecturally right**, or is there a case
   for keeping a text-specialised model for filename work and sharing only the
   vision model? (i.e. drop `minicpm-v`, keep `llama3.1:8b`.)
2. **Is `repeat_penalty 1.3` a real risk for `format: json` output**, or is it
   inert enough at these response lengths not to matter?
3. **What should a benchmark actually measure** to be decisive — sample size,
   corpus composition, and what accuracy delta would justify or veto the switch?
4. **Is there an Ollama-level alternative I've missed** that gets one resident copy
   of the weights while serving different generation parameters per consumer?
   (A second tag from the same blobs appears to load as a *separate* runner and
   therefore double VRAM — please confirm or correct.)
5. **Is the "do nothing" option undersold?** No allocation failure has actually been
   observed — no OOM, no failed unloads, no errors in Ollama's entire log history.
   Is this risk-driven re-plumbing of a working system?

---

## 10. Reproduction commands

```bash
# VRAM
nvidia-smi --query-gpu=memory.total,memory.used,memory.free,utilization.gpu --format=csv

# Models + the custom Modelfile
docker exec ollama ollama list
docker exec ollama ollama show minicpm-v45-frigate --modelfile

# ScanHound's live model settings
docker exec scanhound cat /data/.config/scanhound/config.json

# num_predict override test (run from a container on the proxy network)
curl -s http://ollama:11434/api/chat -d '{
  "model":"minicpm-v45-frigate","stream":false,"format":"json",
  "messages":[{"role":"user","content":"Identify this movie file: The.Matrix.1999.2160p.UHD.BluRay.x265-TERMINAL.mkv. Respond ONLY with a JSON object with keys: title (string), year (integer), confidence (number 0-1)."}],
  "options":{"temperature":0,"num_predict":300,"repeat_penalty":1.0}}'
```
