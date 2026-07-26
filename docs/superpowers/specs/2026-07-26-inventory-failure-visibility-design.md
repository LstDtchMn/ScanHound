# Surfacing scan-failure reasons in the Media Inventory — design

**Date:** 2026-07-26
**Status:** design, awaiting approval. No code changed.

## Problem

The inventory UI shows *that* a title failed but never *why*, so unrelated
failures are indistinguishable on screen. Live production data:

| actual cause | count | what the UI shows |
|---|---|---|
| `filesystem_error` at `stage=stat` — the NAS mounts were down; **since fixed, these would succeed now** | 21 | `failed` |
| `dv_incomplete` at `stage=dovi` — genuine P7 extraction timeouts | 2 | `failed` |

An operator sees "23 failed" and cannot tell that 21 need nothing but a
rescan while 2 are the real Dolby Vision problem. The backend already
distinguishes them correctly — `metadata_scan_items` stores `failure_stage`,
`error_code` and `error_message`, and `GET /plex/metadata-scans/{run_uuid}/items`
returns all three ([database.py:3895](backend/database.py:3895)). The
distinction is discarded at the presentation layer, not at the data layer.

`POST /plex/metadata-scans/{run_uuid}/retry-failures` also already exists
([plex.py:329](backend/api/routes/plex.py:329)) and is unwired in the UI.

## Where the data lives, and why a join is required

`media_inventory` has **no error columns** — its schema stops at
`scan_state`. The reason lives in `metadata_scan_items`, keyed by
`(run_uuid, path)`. The inventory row carries `scan_run_uuid`, which is the
run that last wrote it, so the correct join is:

```sql
LEFT JOIN metadata_scan_items i
  ON i.run_uuid = mi.scan_run_uuid AND i.path = mi.path
```

Joining on the row's own `scan_run_uuid` (rather than the newest run) is what
keeps the reason consistent with the `scan_state` displayed beside it.

**Verified against the live database before specifying:**

```
scan_state=failed    stage=stat   code=filesystem_error   n=21
scan_state=failed    stage=dovi   code=dv_incomplete      n=2
scan_state=current   stage=NULL   code=NULL               n=4
failed rows with no joinable error_code: 0
```

Every failed row joins. `LEFT JOIN` is still correct: a row whose run was
pruned, or an `unscanned` row, must render as "no reason recorded" rather
than disappearing.

## Design

### 1. Backend — extend the existing search, do not add an endpoint

`search_media_inventory` ([database.py:4146](backend/database.py:4146))
already returns a fixed column list from the `inventory_evidence` CTE. Add the
join and three columns: `failure_stage`, `error_code`, `error_message`.

One query change gives the table, the drawer, and any CSV export the same
data, and avoids an N+1 per-row fetch from the drawer.

Filtering is deliberately **not** extended to `error_code` in v1. The existing
`scan_state` facet already narrows to failures, and adding a second failure
axis to the filter vocabulary is scope the problem does not require. The
allowlisted `filter_columns` / `sort_columns` pattern must be preserved
exactly — these are bound parameters and an allowlist specifically to keep the
query non-interpolated.

### 2. Types

```ts
// MediaInventoryItem, additive and optional -- older API responses omit them
failure_stage: string | null;
error_code: string | null;
error_message: string | null;
```

### 3. Table — a reason chip beside the state

`InventoryTable.svelte` renders `scan_state` in the Scan column
([line 23](frontend/src/lib/components/media-inventory/InventoryTable.svelte:23)).
Show `error_code` under it when present, styled like the existing `.state`
chip with `--warning`, matching the current `.unknown` treatment. The mobile
card ([line 35](frontend/src/lib/components/media-inventory/InventoryTable.svelte:35))
gets the same chip in its badge row.

`error_code` is a short slug (`filesystem_error`, `dv_incomplete`) and fits a
chip. `error_message` is free text and must **not** go in the table — it
belongs in the drawer only.

### 4. Drawer — the full reason

`InventoryEvidenceDrawer.svelte` ends with a `<dl class="facts">`
([line 17](frontend/src/lib/components/media-inventory/InventoryEvidenceDrawer.svelte:17)).
When `error_code` is present, add rows for **Failure stage**, **Error code**
and **Error detail**, the last rendering `error_message` with
`class="break-all"` as the File row already does.

The rail's step 2 ("Live file") should also state that the evidence is absent
because the scan failed, rather than reporting `DV unknown · HDR10+ unknown`
as if those were probe results. A failed probe and an inconclusive probe read
identically today, which is the same conflation one level down.

### 5. Retry affordance

Wire `POST /plex/metadata-scans/{run_uuid}/retry-failures` to a button shown
when the current filter is `scan_state=failed` and the visible rows share a
`scan_run_uuid`. Add the client method alongside the other metadata-scan calls
in `client.ts`.

Deliberately scoped: it retries a **run's** failures, which is what the
endpoint does. Per-row retry is not in this design — it would need a new
backend surface.

## Explicitly out of scope

- Any schema change. Everything needed already exists.
- Changing how failures are *classified* — only how they are displayed.
- Automatic rescanning. The 21 stale rows should be retried, but by an
  operator action, not silently on load.
- Per-row retry.
- `error_code` as a filter or sort axis.

## Risks

1. **Stale reason after a partial rescan.** If a later run succeeds for a path
   but the inventory row still points at the older `scan_run_uuid`, the join
   could show an obsolete error. Mitigated because `upsert_media_inventory`
   updates `scan_run_uuid` with the row; a test should pin that a
   failed-then-succeeded path shows no error.
2. **Free-text in the UI.** `error_message` is subprocess output. Render as
   text, never as markup.
3. **Widening a hot query.** Three columns and one indexed LEFT JOIN on the
   inventory list path; confirm the join is on the `metadata_scan_items`
   primary/indexed key rather than a scan.

## Tests

1. A `filesystem_error` row exposes `error_code` through the search API.
2. A `dv_incomplete` row exposes its own distinct code.
3. A `current` row exposes nulls, not an empty string.
4. A row whose `scan_run_uuid` has no matching item still returns (LEFT JOIN),
   with null reason.
5. A path that failed then later succeeded shows **no** error.
6. Table renders the chip only when `error_code` is present.
7. Drawer renders stage / code / detail only when present.
8. `error_message` containing markup is rendered as text.
9. Retry button appears only for a failed-filtered view with a single run.
10. Existing filter/sort allowlists still reject unknown columns.

## Sequencing

Build after the corpus sweep finishes — the 21 stale rows are the natural
first real exercise of the retry path, and rescanning them now would contend
with the sweep for NAS I/O.
