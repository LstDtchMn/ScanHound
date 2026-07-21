# Qualification tooling repairs (2026-07-21)

Claude (git + validation lane) validated this bundle against the REAL
`LstDtchMn/ScanHound` checkout at the code-tested SHA
`a6b4a7b14d6613c27f17de670677ed848fec458d` before any execution, as required by
objective 2/3 ("inspect the bundle against the real repo and repair incorrect
API assumptions before using it"). Two scripts carried assumptions that do not
match the shipped code; both are repaired here. The other five scripts
(`00_preflight`, `01_snapshot_db`, `02_migration_matrix`, `03_filesystem_sentinel`,
`06_finalize_evidence`) were verified correct against the real code and are
unchanged.

## 1. `scripts/04_settings_guard.py` — RSS fields are not `/settings` fields

**Defect.** The original applied each stage by `PUT /settings` of the whole
desired dict, including `hdencode_discovery_mode` and the `hdencode_rss_*`
booleans. ScanHound's `SettingsUpdate` model
(`backend/api/routes/settings.py`) is declared `model_config =
ConfigDict(extra="forbid")` and does **not** contain any of those keys, so the
request would return HTTP 422 and change nothing — the guard could never arm the
disabled or shadow stage.

**Real control surfaces (verified in the checkout).**

- `PUT /settings` accepts, of this tool's desired state, only:
  `auto_rename_enabled`, `auto_grab_enabled`, `hdencode_enabled`,
  `background_scan_enabled` (all present in `SettingsUpdate`).
- `hdencode_discovery_mode` is set through `POST /rss/mode`
  (`backend/api/routes/rss.py::set_rss_mode`), body
  `{"mode": "listing" | "rss_shadow" | "rss_primary"}`. `rss_primary` requires
  completed shadow readiness (409 otherwise); `listing`/`rss_shadow` do not.
  This tool never selects `rss_primary`.
- `hdencode_rss_shadow_compare_enabled`, `hdencode_rss_auto_grab_enabled`,
  `hdencode_rss_listing_fallback_enabled` have **no write endpoint**. They are
  deploy-time config (`backend/config.py` `DEFAULT_CONFIG`) whose defaults
  already equal the qualification-required values (shadow_compare=`True`,
  auto_grab=`False`, listing_fallback=`False`).

**Repair.** PUT only the four writable keys; set the mode via `POST /rss/mode`;
verify the three unsettable RSS flags via `GET /settings` (which returns the
full masked config) and report a distinct `unsettable_mismatch` if the running
config diverges, instead of issuing a PUT that would 422. HTTP errors are
surfaced with method/path/status/body. The `--stage`, `--execute`,
`--evidence-dir` interface is unchanged, so `runbook/EXECUTION_ORDER.md` steps 5
and 7 run verbatim.

## 2. `scripts/05_shadow_evidence.py` — wrong shadow-cycle columns

**Defect.** The original introspected any `hdencode%`/`%shadow%`/`%comparison%`
table and guessed column names. Against the real
`hdencode_shadow_cycles` schema (`backend/database.py`) that meant:

- completion was tested against `complete`/`completed`/`is_complete`, none of
  which exist — so every introspected row (including `hdencode_shadow_misses`
  rows) defaulted to "complete", overcounting comparison cycles and letting the
  ≥20-cycle gate pass on non-cycles;
- recovery was tested against `recovery_observed`/`catchup_observed`/
  `is_recovery`, none of which exist — so recovery was always 0.

A relevant miss is a mandatory stop condition, so mis-reading these columns is
safety-relevant.

**Repair.** Compute readiness directly from the authoritative columns, mirroring
`Database.get_hdencode_shadow_summary` / `get_hdencode_rss_readiness`:

- completed cycle = `hdencode_shadow_cycles` row with
  `outcome IN ('success','relevant_miss')` and `normal_feeds_complete=1`;
- relevant misses = `SUM(relevant_miss_count)` (cross-checked against the
  `hdencode_shadow_misses` row count);
- recovery = `restart_recovery=1 OR catchup_used=1`;
- request reduction = `100*(listing_requests-rss_requests)/listing_requests`;
- normal feeds healthy = `movies_all` and `tv_all` in `hdencode_feed_state`
  with `last_status IN (200,304)`, zero consecutive failures, and a fresh
  `last_checked_at`.

The `--db`/`--evidence-dir` interface is unchanged (step 8 runs verbatim). New
**optional** `--base-url`/`--token` additionally capture the app's own
`GET /rss/status` readiness and reconcile it against this independent
computation.

## Validation

`scripts/selftest.py` (standard library only; no network, Docker, or checkout)
proves both repairs and passed on Python 3.12.9:

- **04** — a stub server emulating `extra="forbid"` confirms the disabled and
  shadow stages apply with no 422 and verify correctly, and that a naive
  whole-dict PUT is in fact rejected (so the emulation is meaningful).
- **05** — a synthetic database with the real columns confirms completed
  cycles, recovery cycles (3, not 0), and relevant misses are read correctly,
  and that a single `relevant_miss` flips readiness off.

No ScanHound application code (`backend/`, `frontend/`) was changed by these
repairs, so the code-tested regression at `a6b4a7b` (backend 3974/0, frontend
check/vitest/build green) is unaffected.
