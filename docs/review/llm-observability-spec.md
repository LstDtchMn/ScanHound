# Spec — LLM identification observability (Option E)

**Date:** 2026-07-27
**Author:** Claude (specification)
**Implements:** Option E from the Ollama consolidation peer review, rounds 1–3
**Prerequisite:** RSS shadow qualification window must close first (~2026-07-28).
No identification-path change may land before it does.
**Scope:** observability only. **No model routing change. No timeout change. No
prompt change. No `num_ctx` change.** Those are separately attributable decisions
and must not be bundled in.

---

## 1. The problem, stated precisely

Every LLM identification failure in ScanHound is invisible in production.

1. All 8 log statements in `backend/rename/llm_identify.py` are `logger.debug`
   (L79, 283, 295, 333, 442, 672, 752, 831). `setup_logging()`
   (`app_service.py:268-324`) sets the root logger **and both handlers explicitly**
   to `INFO` unless `debug_mode` is true; the live container has
   `debug_mode = False`. Those records are therefore **never emitted** — measured:
   0 `[DEBUG]` records in 23,456 production log lines.
2. All six Ollama lanes use a bare `except Exception` and `return None`. There is
   no `except Timeout`, no `except RequestException`, no `except JSONDecodeError`.
   A genuine defect (`KeyError`, `AttributeError`, `TypeError`) is swallowed
   identically to a network timeout.
3. **The core defect: three semantically different outcomes collapse into one
   `return None`** — transport/HTTP failure, a legitimate model abstention, and
   "precondition not met" (not configured, no ffmpeg, no subtitles, no tesseract).
   A lane that never made a network call is indistinguishable from a lane that
   made 12 calls and got 12 timeouts.
4. Callers discard it silently. `_apply()` (`service.py:1013-1016`) opens with
   `if not res or not res.get("title"): return False`. There is no `else` branch at
   `service.py:549`, `1042`, `1054`, `1073`, or `episodes.py:89`. The only records
   written are on **success** (`rlog.info` at `service.py:1031` and `558`).

**Consequence:** no benchmark can distinguish a model regression from a truncated
prompt from a swallowed defect, and the running system cannot report its own
failures.

---

## 2. Design decisions

### 2.1 Records go to `rlog`, not `logger`

`rlog` (`scanhound.rename`, pinned to `INFO` at `service.py:58`) is the existing
per-file decision trace. It writes pipe-delimited lines to both `scanner.log` and
stdout, and it is **always on in production today**.

This is the single most important decision in this spec: routing outcome records
through `rlog` makes them visible **without touching `debug_mode`**, which is inert
until a container restart anyway (`PUT /settings` never re-invokes `setup_logging()`).

`logger.debug` diagnostics may stay where they are as detail-level breadcrumbs, but
they are **not** the deliverable and nothing may depend on them.

> `rlog` is pinned to INFO and can never emit DEBUG. Outcome records must therefore
> be `.info()` or `.warning()`.

### 2.2 A closed outcome taxonomy

Every lane must resolve to exactly one of these. This replaces the ambiguous `None`.

| Outcome | Meaning | Level |
|---|---|---|
| `ok` | identified, result adopted or offered | INFO |
| `no_identification` | model responded validly and abstained (null/blank title) | INFO |
| `not_configured` | `base_url`/`model` missing — no call attempted | INFO (once per operation) |
| `precondition_unmet` | no ffmpeg / no tesseract / no subtitle track / clip too short / insufficient text | INFO |
| `timeout` | `requests.Timeout` | WARNING |
| `transport_error` | `requests.RequestException` other than timeout | WARNING |
| `http_error` | non-2xx from `raise_for_status()` | WARNING |
| `invalid_response` | malformed JSON, fence-strip failure, schema mismatch | WARNING |
| `unexpected_exception` | anything else — a real defect | ERROR + `logger.exception()` |

**`no_identification` is not a failure.** Logging it as one will make the metrics
useless — the prompts explicitly instruct the model to answer `title: null` when
unsure (`_VISION_SYSTEM` L177-183, `_SUBTITLE_SYSTEM` L341-348, `_OCR_SYSTEM`
L448-455).

### 2.3 Fail-safe behaviour is preserved, unchanged

The contract stays: **any LLM failure yields `None` and the caller falls back
cleanly.** This spec adds visibility, it does not add raising.

Typed handlers are inserted **before** a final `except Exception` which is retained:

```python
except requests.Timeout as exc:
    _record(lane, "timeout", exc=exc, elapsed=...)
    return None
except requests.RequestException as exc:
    _record(lane, "transport_error", exc=exc, elapsed=...)
    return None
except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
    _record(lane, "invalid_response", exc=exc, elapsed=...)
    return None
except Exception as exc:                       # MUST be retained
    logger.exception("Unexpected Ollama failure in %s", lane)
    _record(lane, "unexpected_exception", exc=exc, elapsed=...)
    return None
```

`resp.raise_for_status()` raises `requests.HTTPError`, a subclass of
`RequestException` — classify it as `http_error` by checking
`isinstance(exc, requests.HTTPError)` inside the `RequestException` handler, or add
an explicit `except requests.HTTPError` **before** it.

### 2.4 One record per operation, not per attempt

`identify_from_frames` loops up to `_FRAME_MAX = 12` frames at 45 s each. Twelve
warnings for one file is unusable.

**Accumulate per-attempt outcomes in a list; emit one summary line per operation**,
with per-attempt counts rolled up. Per-attempt detail stays at `logger.debug`.

---

## 3. Required changes

### 3.1 `backend/rename/llm_identify.py`

**New imports** (the module currently imports only `json`, `logging`, `re`,
`typing`, `requests`):

```python
import time
```

**New module-private helper**, placed after the logger at L20:

```python
def _record(lane: str, outcome: str, *, elapsed: float = 0.0,
            resp_json: Optional[dict] = None, exc: Optional[BaseException] = None,
            attempts: int = 1, detail: str = "") -> None:
```

It must emit **one** line through `rlog` and must not raise. Import `rlog` lazily
inside the function or accept it as a parameter — `llm_identify.py` must not import
`service.py` (circular import risk); prefer
`logging.getLogger("scanhound.rename")` directly.

**Fields to record** (see §4 for the privacy rules):

`lane`, `outcome`, `model`, `attempts`, `elapsed_s`, `http_status`,
`done_reason`, `total_duration`, `load_duration`, `prompt_eval_count`,
`eval_count`, `response_len`, `exc_type`.

> **`total_duration` is required, not optional.** Round-3 measurement found the
> worst end-to-end request (29.658 s) did **not** span a cold load — it was a warm
> request with slow generation. `load_duration` alone would have shown nothing.

**Per-lane edits** — apply the §2.3 handler stack to all six:

| Lane | Function | Except block to replace |
|---|---|---|
| 1 | `identify` | L78-80 |
| 2 | `_ask_vision` (nested in `identify_from_frames`) | L282-284 |
| 3 | `identify_from_subtitles` | L441-443 |
| 4 | `identify_from_credits_ocr` | L671-673 |
| 5 | `disambiguate_episode` | L751-753 — **see structural fix below** |
| 6 | `_ollama_page_hints` | L830-832 — **see note below** |

**Structural fix — `disambiguate_episode` (L709-753).** Its `except` block does
**not** return; it falls through to a shared trailing `return None` which is *also*
reached on a successful HTTP call whose parsed season/episode were rejected.
Success-but-rejected and exception are currently structurally identical. Give the
except block its own `return None` so the two paths are separable.

**A seventh `except Exception` exists at L696, in `test_connection`.** It is
deliberately excluded from this spec: it is a connectivity probe, not a generation
lane, and it already surfaces the error by returning `{"ok": False, "error": str(e)}`
rather than swallowing it. Leave it alone.

**Note on lane 6 — `_ollama_page_hints` is dead code in production.**
`extract_page_hints(page_text, *, base_url="", model="")` is called positionally
with only `full_text` at `detail_scraper.py:383`, so the guard
`if base_url and model:` (L798) is always False. Instrument it for consistency, but
**do not** count it in any live metric, and do not "fix" the call site — that is a
behaviour change outside this spec's scope.

**Precondition guards must record too.** These currently return `None` with no
trace at all. Each needs a `precondition_unmet` or `not_configured` record:
`identify` L48-49; and the ffmpeg/tesseract/subtitle/duration guards in
`identify_from_frames`, `identify_from_subtitles`, `identify_from_credits_ocr`.

**Abstention must be recorded.** `_normalize` (L83-104) returns `None` for a
non-dict or a blank title — a valid abstention reached from **inside** the `try`,
never touching the except. Each lane must distinguish "call succeeded, model
abstained" (`no_identification`) from "call failed".

### 3.2 `backend/rename/service.py`

**Do not attempt to write to the job row from inside the LLM rungs.** The `job`
dict is first built at L1205 and inserted at L1210/L1344, while every `_llm.*` rung
fires at L997 and L1039-1075. **There is no `job_id` in flight.**

Accumulate outcomes in a local list alongside `match`, then emit a single
per-operation summary when the job is composed.

**The every-rung-failed case must be covered.** L1208-1210 early-returns:

```python
if not match:
    job.update(status="needs_review", warning_message="No confident match found")
    return self._create(job)
```

This is precisely the "everything failed" path and it bypasses everything below it.
The summary must be attached **in this branch too**, not only on the happy path.

Add an `else` (or explicit miss record) at the four call sites: L549, L1042, L1054,
L1073, plus `episodes.py:89`.

**Match the existing line shape.** `rlog.info("media  | %s | %s -> %r (%s) conf=%.0f", …)`
at L1031 is the house style — pipe-delimited, fixed-width lane label. New records
should read as siblings of it, e.g.:

```
llm    | subtitle | no_identification | 0.97s | attempts=1 | eval=26
llm    | vision   | timeout           | 45.02s | attempts=12/12 | ok=0
```

---

## 4. Privacy rules

**Must never be logged at INFO or WARNING:** OCR text, subtitle dialogue, base64
image data, full model responses, licence plate strings, recognised person names.

Log **lengths and counts** instead: `response_len`, `prompt_eval_count`,
`ocr_chars`, `frames_tried`.

**Filenames are already logged** by the existing `rlog.info` at `service.py:1031`,
so including a filename in an outcome record adds no new exposure category and
keeps records correlatable with existing lines. Do **not** treat this as licence to
add the content categories above.

The existing credential-masking filter in `setup_logging()` covers tokens and
passwords only — **nothing scrubs paths or titles.** Do not rely on it.

---

## 5. Tests

Canonical command: `python -m pytest tests/ --tb=short -q`
Coverage gate: `--cov=backend --cov-fail-under=40` (py3.11 only).

### 5.1 Two existing tests will break if the catch-all is narrowed away

- `tests/test_llm_identify.py::test_returns_none_on_ollama_error` (L25)
- `tests/test_llm_identify.py::test_falls_back_to_regex_on_ollama_failure` (L87)

Both inject a **bare `Exception(...)`**, not a `requests` exception, and assert
graceful degradation. A third in `test_rename_service.py` does likewise.

**Retaining the final `except Exception` (§2.3) keeps all three passing** — the bare
exception falls through the typed handlers to the catch-all, is logged via
`logger.exception`, and still returns `None`. This is a required property, not an
accident. Do not narrow it away.

### 5.2 New tests required

1. Each outcome in §2.2 is produced for its triggering condition.
2. `no_identification` (200 OK, `title: null`) is **not** recorded as a failure.
3. `unexpected_exception` produces an ERROR record **and** still returns `None`.
4. The vision lane emits **one** operation summary, not twelve.
5. No OCR text, subtitle dialogue, base64 data, or full model response appears in
   any record at INFO or above.
6. Timeouts remain fail-safe — `None` returned, caller falls back.

### 5.3 House conventions — do not deviate

- **No HTTP-mocking library exists in this repo.** `responses`, `requests-mock` and
  `respx` are absent from the source tree *and* from `requirements.txt`. CI installs
  test deps by hand at `.github/workflows/tests.yml:42-52` and does **not**
  `pip install -r requirements.txt` for the backend job. **Use
  `unittest.mock.patch` only.** Adding a dependency means editing CI too.
- Stub pattern in use: `patch("requests.post", return_value=MagicMock())` with
  `.json.return_value` set and `.raise_for_status` reassigned to a `MagicMock()`.
- **`patch("requests.post")` is global and works only because `llm_identify.py:18`
  does `import requests`.** If the implementation switches to
  `from requests import post`, every existing mock silently stops intercepting.
  **Keep `import requests`.**
- Log assertions must use `caplog.at_level(logging.INFO, logger="scanhound.rename")`
  for `rlog` records. `caplog` defaults to WARNING, so a naive
  `assert "…" in caplog.text` passes vacuously.
- `shutil`, `subprocess`, `tempfile`, `base64` are imported **inside function
  bodies** (L113, 142, 217-220, 370-372, 469, 490-493, 594, 680).
  `patch.object(llm_identify, "shutil", …)` raises `AttributeError` — patch globally.
- `tests/test_llm_identify.py` uses **no** fixtures; `conftest.py`'s 5 autouse
  fixtures still apply.

---

## 6. Out of scope

- Model routing (Options B/C) — deferred pending benchmark.
- `num_ctx` — leave unset. Measured at 35.9% context usage on a max-size OCR
  payload; no action warranted.
- Timeout values — do not change. The round-3 finding (warm-generation variance,
  vision lane at ~1.5× margin) is a **telemetry target**, not a licence to retune
  blind. Change them only on collected evidence.
- Prompts, `_FRAME_MAX`, the `[:4000]` OCR cap.
- DB persistence of outcomes — **Phase 2.** Blocked by the missing in-flight
  `job_id` (§3.2). Log records are the shippable increment.
- Fixing `_MATCH_SOURCE_LABELS` (`service.py:150-157`) — its keys
  (`fuzzy/vision/ocr/subtitle/multi/imdb`) match none of the values actually
  produced (`llm/llm_subtitle/ocr_credits/llm_vision/…`), so the
  "Matched using …" reason at L177 never renders. **Real bug, separate PR.** If
  this spec's implementation needs a lane label, reuse or fix that dict rather than
  introducing a third naming scheme.

---

## 7. Acceptance

1. `python -m pytest tests/ --tb=short -q` passes; coverage ≥ 40%.
2. The three bare-`Exception` tests still pass unmodified.
3. With `debug_mode = False` (production default), a forced Ollama failure produces
   a visible `rlog` record in `scanner.log` **and** stdout.
4. A forced abstention produces `no_identification`, not a failure.
5. A forced 12-frame vision failure produces exactly one summary line.
6. No content from §4's prohibited list appears at INFO or above.
7. Model routing, timeouts, prompts and `num_ctx` are byte-for-byte unchanged.
