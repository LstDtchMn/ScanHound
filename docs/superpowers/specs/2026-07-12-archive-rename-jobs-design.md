# Archive Rename Jobs — Design Spec

**Date:** 2026-07-12
**Status:** Approved (design), pending implementation plan

## Goal

Give the Match and Rename (Renames) page a way to get completed/handled jobs out of the active queue without losing them — both automatically (the instant a job is applied) and manually (any job, any status, via a selector), with a dedicated place to see and restore what's been archived.

## Scope decisions

- **Both triggers are in scope**, per explicit user confirmation: automatic archiving of applied jobs, AND a manual archive action that works on any job in the list regardless of status — identified/matched, needs-review, failed, or already applied.
- **Auto-archive timing: immediate.** The moment a job's status flips to `applied`, it's archived in the same update — the active queue never shows a job that's done but still cluttering the list.
- **Archived jobs get a dedicated view with restore**, mirroring this project's existing `dismissed_items`/Skipped-manager pattern: a new "Archived" tab on the Renames page's status dashboard, with per-job and bulk Unarchive.
- **Desktop only for this round.** Mobile's Renames view is a fundamentally different UI (a stats dashboard + swipe-card review deck, not the desktop's row list/table) — its swipe deck already only surfaces actionable (non-applied) jobs by design, so auto-archive mostly just means mobile's counts drop faster. No mobile UI changes in this pass.
- **Copy is deferred to implementation time**, drafted by the Fable model tier (per standing model-tiering practice) once the plan exists — button labels, tooltips, toast text, and the Archived tab's empty state are not fixed by this spec.

## Data model

Add a nullable `archived_at TIMESTAMP` column to the existing `rename_jobs` table via an idempotent, guarded `ALTER TABLE` (the same pattern already used for `dismissed_items`' extra columns — attempt the ALTER, tolerate only "duplicate column name", re-raise anything else).

Archiving is **orthogonal to `status`**. A job keeps whatever real status it has (`pending`/`matched`/`needs_review`/`applying`/`applied`/`failed`); archiving only sets or clears this timestamp. An archived `failed` job and an archived `applied` job are both just "archived," shown together in the Archived view.

`list_rename_jobs()` gains an `archived: bool = False` parameter. Every existing caller — the five current status-filter tabs (all/needs_review/matched/applied/failed), status counts, anything that lists jobs today — implicitly stays `archived=False`, so none of today's behavior changes by default. The new Archived tab is the one caller that passes `archived=True`, and does so **without** also filtering by a specific `status` — it shows every archived row regardless of the status underneath.

The existing duplicate-file dedup check (`rename_job_exists_for_path`, keyed on `original_path`) is **explicitly unchanged** — it still matches archived rows exactly as before. Archiving a job (resolved or not) must never cause the scanner to treat the same file as newly discovered on a later scan.

## Backend

- **Auto-archive on apply.** Both places in `backend/rename/service.py`'s `apply()` that set `status="applied"` — the same-inode no-op success path and the main move-success path — also set `archived_at` to now in that same `update_rename_job()` call, so a job is never visibly "applied but not yet archived." Implementation must grep for every `status="applied"` (or `status='applied'`) assignment across the codebase (not just these two known sites — e.g. the auto-rename background thread in `main.py` may set it too) and archive at each one, so no code path leaves an applied job un-archived.
- **Manual archive/unarchive: one toggle endpoint**, `POST /rename/archive`, body `{ids: number[], archived: bool}` — mirrors the existing `POST /results/dismiss` boolean-flag shape exactly (`true` archives, `false` unarchives the same set of ids).
- New `DatabaseManager` methods: `archive_rename_jobs(job_ids)` / `unarchive_rename_jobs(job_ids)` — bulk `UPDATE rename_jobs SET archived_at = ...` / `= NULL` over the given ids.
- The manual archive action silently skips any job in the given id set whose current status is `'applying'` (archiving the rest of the batch normally) — the transient mid-move state is never archived, but one in-flight job in a mixed selection must not block archiving the others. This mirrors the established "one bad item never aborts the whole batch" pattern used elsewhere in this codebase (e.g. the Plex library metadata-scan job's per-file failure isolation).

## Frontend (desktop only)

- `StatusDashboard` gains a sixth tab, **Archived**, alongside the existing all/needs_review/matched/applied/failed. Selecting it requests `archived=true` (not the default `archived=false` every other tab uses) and does not additionally constrain by `status`.
- `BulkBar` gains an **Archive** action, enabled whenever the current multi-select contains at least one job not in `'applying'` status — same enable/disable pattern already used by the page's other bulk actions (`bulkBusy`/`applyActive`).
- While viewing the Archived tab: each row gets an **Unarchive** button (single-job restore); the same multi-select mechanism gets a bulk **Unarchive** action when one or more archived jobs are selected. Both call the same `POST /rename/archive` toggle endpoint with `archived: false`.
- All new copy (button labels, tooltips, toast messages, the Archived tab's empty state) is drafted at implementation time by the Fable model tier, not fixed here.

## Testing

- **Backend:** `archive_rename_jobs`/`unarchive_rename_jobs` round-trip (archive then `list_rename_jobs(archived=True)` finds it; unarchive then the default `list_rename_jobs()` finds it again, with no explicit `archived` arg needed since `False` is the default). `list_rename_jobs()` with its default arguments never returns an archived row regardless of that row's `status`. Both `apply()` success paths leave `archived_at` set — regression-guard each call site individually, not just one representative case. The toggle endpoint does not archive a job whose status is `'applying'`. A regression test confirms `rename_job_exists_for_path()` still finds archived rows (dedup-on-rescan behavior is unaffected by archiving).
- **Frontend:** `BulkBar`'s Archive action is disabled only when every selected job is `'applying'`, enabled otherwise. Selecting the Archived tab renders jobs of mixed underlying status together. Unarchive (single row and bulk) round-trips a job back into the default (non-archived) view.

## Non-goals (YAGNI)

- No mobile UI changes in this pass (see Scope decisions).
- No configurable auto-archive delay or opt-out toggle — auto-archive-on-apply is immediate and unconditional, per the explicit choice made during design.
- No new schema beyond the single `archived_at` column — no separate archive table, no duplicated schema (rejected as over-engineered relative to this app's actual job volume).
- No change to the existing `rename_job_exists_for_path` dedup semantics.
