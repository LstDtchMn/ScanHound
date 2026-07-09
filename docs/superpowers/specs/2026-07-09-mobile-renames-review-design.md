# Mobile Renames Review — Design

**Status:** Approved (design phase). v1 scope.
**Date:** 2026-07-09

## Goal

Make the Renames screen usable on a phone by turning the crammed desktop list
into a **focused, one-at-a-time review of the items that need a decision**. The
current mobile screen renders the desktop row/grid: the match confidence and the
warning are truncated or absent, filenames are clipped, and tap targets are tiny.
The redesign splits the two jobs of the screen — clear the confident matches fast,
and *scrutinize* the uncertain ones carefully — into a summary hero plus a
full-screen review deck.

## Architecture

Everything the deck needs already exists server-side. `RenameJob` (from `GET
/rename/jobs`) carries `match_confidence` (0–100), `warning_message`,
`destination_conflict`, `keep_recommended`/`keep_reason`, `dv_layer`,
`match_reasons`, and the contextual `combined_episode` / `suggested_correction`.
Every per-job action already has an endpoint: `apply`, `undo`, `reidentify`
(auto re-run), `rematch` (search-TMDB flow), `accept-combined`,
`accept-correction`, `delete`, and `bulk/apply(ids)`. This feature is therefore a
**mobile UI addition only — no backend changes.**

The Renames route forks on `isPhone` (the same store the Scan page uses:
`max-width:767px` AND `pointer:coarse`). On a phone, the desktop review chrome —
the list/grid, `StatusDashboard`, `RenameFilterBar`, and `BulkBar` — is replaced
by a **summary hero** plus a **full-screen review deck**. `RenamesHeader`
(Process ▾ / Dolby Vision / Re-identify all), the Dolby Vision scan surface, and
`TrashPanel` are kept as-is and rendered below the hero, so no functionality is
lost. The desktop layout is untouched.

## Tech Stack

SvelteKit 5 (runes), the existing `renames` store and `api` client, the existing
`RematchModal`, the WebSocket `rename:job` / `rename:queue` events. Deploy via
`docker compose up -d --build` only.

## Global Constraints

- Deploy in-app changes ONLY via `docker compose up -d --build`.
- **No backend changes.** The design uses only endpoints that already exist.
- The desktop `/renames` page and desktop navigation must be unchanged; the fork
  is gated strictly on `isPhone`.
- "Ready to apply" means **`match_confidence >= 100`**, not the server's ≥95%
  apply-confident threshold — the user's rule is "manually review anything under
  100%."
- Reuse existing store actions (`applyJob`, `deleteJob`, `acceptCombinedJob`,
  `acceptCorrectionJob`) and `RematchModal`; do not duplicate their logic.
- Works in both the responsive web app and the Tauri/Android wrapper.
- Tests accompany each unit; deploy only after the changed-module suite is green.

---

## Job classification (shared helper)

A single pure function classifies each `RenameJob` into one bucket, used by both
the summary counts and the deck's scope filter. Put it in
`frontend/src/lib/renames/review.ts` so it is unit-testable in isolation.

- **`ready`** — `status === 'matched'` AND `(match_confidence ?? 0) >= 100` AND
  no `warning_message` AND not `destination_conflict`.
- **`needsReview`** — still-active (not `applied`/`reverted`/`pending`) AND NOT
  `ready`. Concretely: `status === 'needs_review'`, `status === 'failed'`, or a
  `matched` job with confidence < 100, a `warning_message`, or a
  `destination_conflict`.
- **`inactive`** — `applied`, `reverted`, or `pending` (excluded from both counts
  and the deck; `pending` = matcher hasn't run yet).

```ts
export type ReviewBucket = 'ready' | 'needsReview' | 'inactive';
export function classifyJob(job: RenameJob): ReviewBucket;
export function partitionJobs(jobs: RenameJob[]): {
  ready: RenameJob[];
  needsReview: RenameJob[];
};
```

Deck scope maps to this: **Under 100%** = `needsReview`; **All** = `ready`
concatenated with `needsReview` (ready first, so "All" reads as the full active
queue). Ordering within each list: by `match_confidence` ascending (lowest-
confidence first — the ones most needing scrutiny lead), nulls first.

---

## Components

### 1. `MobileRenamesView.svelte` (new)

Rendered by `renames/+page.svelte` when `$isPhone`. Owns the phone review
surface and the deck-open state.

- **On mount:** reuse the page's existing `loadRenameJobs()` /
  `loadRenameStatus()` / `loadDvScans()` calls (lift them so both forks share
  them, or call from this component's `onMount`).
- **Applying banner:** keep the existing `$renameQueue` progress banner (background
  apply progress) at the top — unchanged markup.
- **Search:** a single text field bound to the existing `renameQuery` store; it
  filters the active set feeding both the counts and the deck. Move the page's
  inline `matchesQuery(job, q)` into `review.ts`, export it, and import it in both
  the desktop page and this component (single source of truth).
- **Summary hero:** two cards computed from `partitionJobs($renameJobs)` filtered
  by the search query:
  - **Ready** card (success tint): count + "ready to apply · 100%" + **Apply all**
    button → applies exactly the ready IDs via `api.bulkApply(readyIds)` then
    `refreshRenames()`. Hidden when the ready count is 0.
  - **Needs review** card (warning tint): count + "need review · under 100%" +
    **Review** button → opens the deck at scope `needsReview`. Hidden when 0.
  - **Scope toggle** (segmented, pill): `Under 100% · N` (default) / `All · M`.
    Sets the scope the Review button (and deck) will use.
- **Empty states:** no jobs at all → "No rename jobs yet. Use Process ▾ to scan a
  folder." (points at `RenamesHeader`'s menu). Jobs exist but none need review →
  "All clear — N ready to apply" with the Apply-all button.
- **Ancillary tools (kept):** render `RenamesHeader` (its Process / Dolby Vision /
  Re-identify-all menu), the existing Dolby Vision scan surface (`#dv-scan-surface`
  disclosure), and `TrashPanel` below the hero. These are reused verbatim, not
  redesigned.

**Depends on:** `renameJobs`, `renameQuery`, `renameQueue`, `renameStatus`,
`refreshRenames`, `api.bulkApply`, `classifyJob`/`partitionJobs`, `isPhone`.
**Interface:** no props.

### 2. `RenameReviewDeck.svelte` (new)

A full-screen overlay (fixed inset, high z-index, `env(safe-area-inset-*)`
padding — same overlay pattern as the mobile Scan `DetailSheet`). Manages the
queue, current index, scope, and navigation. Renders one `RenameReviewCard` at a
time.

- **Props:** `jobs: RenameJob[]` (the current active set, already search-filtered),
  `initialScope: 'needsReview' | 'all'`, `onClose: () => void`.
- **Derived queue:** `queue = scope === 'needsReview' ? needsReview : [ready,
  needsReview]` from `partitionJobs(jobs)`, in the ordering above. A scope toggle
  in the deck header switches live.
- **Header:** close (×), the scope segmented toggle, and a position indicator
  `n / N`.
- **Navigation:** previous/next arrows and horizontal **swipe** (reuse the Scan
  page's `gestures.ts` drag machine). After an action that resolves an item
  (**Apply**, **Remove**, **Accept …**), **auto-advance** to the next item.
- **Live sync:** the deck reads from the reactive `jobs` prop. When a `rename:job`
  WS event updates the store, an applied/removed item leaves the active set; the
  deck clamps the index and, if the current item disappeared, shows the item now
  at that index (or the completion state if the queue emptied).
- **Completion state:** when the queue is exhausted → "All reviewed" with a
  **Done** button (`onClose`) and, if any ready items remain, an **Apply all**
  shortcut.
- **Rematch:** holds the `rematchJob` state; **Rematch** on a card sets it and the
  deck renders the existing `RematchModal job={rematchJob}` on top. On close/
  confirm, `refreshRenames()` runs and the deck re-derives.

**Depends on:** `partitionJobs`, `gestures.ts`, `RematchModal`, `refreshRenames`,
the `renames` store actions. **Interface:** the props above.

### 3. `RenameReviewCard.svelte` (new)

Pure presentation for one job; emits action callbacks. No data fetching.

- **Header row:** poster (`poster_url`, `ti-photo` placeholder when null), title +
  `(year)`, and the **confidence** as the visual hero — large, colored by
  `confidenceVariant()` (green 100, amber mid, red low). Below it, `match_source`
  ("matched by filename / llm / manual") and a `dv_layer` badge when present
  (reuse the DV badge styling from the page).
- **Confidence → reasons:** tapping the confidence figure expands `match_reasons`
  ("why this is under 100%") inline — the same reasons `BadgeCluster` already
  surfaces. No popover; an expand/collapse block (mobile-friendly).
- **From → To:** the full `original_filename` and `new_filename`, monospace,
  wrapped (`word-break: break-all`), never truncated. Labelled "From" / "To".
- **Issue banner:** when `warning_message` is present, a danger-tinted block shows
  the **full** text. When `destination_conflict`, append the `keep_reason` line
  and note whether this release is the recommended keep (`keep_recommended`). No
  overwrite/keep-both controls — those have no backend; the issue is *surfaced*
  and resolved via the actions below.
- **Actions (only render those valid for the job's status):**
  - **Apply** (primary) — `status` `matched` or `needs_review` → `applyJob(id)`.
  - **Rematch** (primary) — always available on an active job → emits
    `onRematch(job)` (opens `RematchModal`). This is the manual "fix the
    identification" path; there is intentionally no free-text filename edit.
  - **Re-identify** (secondary) — `needs_review` or `failed` →
    `api.reidentifyRename(id)` (auto re-run of the matcher).
  - **Accept {code}** (secondary, contextual) — only when `combined_episode` →
    `acceptCombinedJob(id)`; only when `suggested_correction` →
    `acceptCorrectionJob(id)`.
  - **Remove** (secondary / under a ⋯ affordance) — `deleteJob(id)`.
  - A `busy` state disables the buttons for the in-flight job.

**Depends on:** `confidenceVariant` (from `$lib/constants`), the DV badge map,
`renames` store actions, `api.reidentifyRename`. **Interface:** `job: RenameJob`,
`busy: boolean`, and callbacks `onApply`, `onRematch`, `onReidentify`,
`onAcceptCombined`, `onAcceptCorrection`, `onRemove`.

### 4. `renames/+page.svelte` fork (modify)

Wrap the existing review chrome in `{#if $isPhone}<MobileRenamesView … />{:else}
… existing markup … {/if}`. The `onMount` loaders and the ancillary surfaces
(`RenamesHeader`, DV, `TrashPanel`) are shared; only the list/grid +
`StatusDashboard` + `RenameFilterBar` + `BulkBar` block is gated to the desktop
branch. No desktop behavior changes.

---

## Data Flow

1. `loadRenameJobs()` populates `renameJobs`; the WS `rename:job` handler keeps it
   live (existing).
2. `MobileRenamesView` derives `{ready, needsReview}` via `partitionJobs`
   (search-filtered) → renders the summary counts and scope toggle.
3. **Apply all** → `api.bulkApply(readyIds)` → jobs queue server-side, land over
   `rename:job`, and drop out of the active set on the next reactive update; the
   `$renameQueue` banner shows progress.
4. **Review** → opens `RenameReviewDeck` at the chosen scope.
5. In the deck, a per-card action calls the matching store action / endpoint →
   `refreshRenames()` (or the WS event) updates the store → the item leaves the
   queue → the deck auto-advances.
6. **Rematch** → `RematchModal` runs its own search → confirm → `refreshRenames()`.

## Error Handling

- Every action wraps in try/catch and surfaces a toast, mirroring the desktop
  page's `run()` pattern (set `busy = id` → await → success/error toast → clear).
  Implement this wrapper in `RenameReviewDeck` (which owns the busy state and
  passes it to the card); the card itself only emits callbacks. The `busy` guard
  prevents double-taps.
- If the active set empties while the deck is open, it shows the completion state
  rather than a blank card; index is always clamped to the queue length.
- Apply is queued server-side (cross-device moves take minutes); the deck advances
  optimistically and the `$renameQueue` banner + `rename:job` events reflect the
  real outcome. A failed apply reappears as a `failed` job on refresh.

## Testing

- **`review.ts` (pure):** `classifyJob` for each bucket boundary — matched-100/no-
  warning → ready; matched-100 **with** a warning or conflict → needsReview;
  matched-99 → needsReview; `needs_review`/`failed` → needsReview;
  `applied`/`reverted`/`pending` → inactive. `partitionJobs` ordering
  (confidence-ascending, nulls first) and the scope mapping (Under-100 vs All).
- **`MobileRenamesView`:** counts reflect `partitionJobs` under a search filter;
  Apply-all calls `bulkApply` with exactly the ready IDs; hero hides the ready /
  needs-review card at count 0; the all-clear and no-jobs empty states.
- **`RenameReviewDeck`:** queue derivation per scope; auto-advance after a
  resolving action; index clamp when the current item disappears; completion state
  when empty; Rematch opens `RematchModal`.
- **`RenameReviewCard`:** renders only the status-valid actions; confidence color
  bucket; reasons expand; full (untruncated) from/to and warning text; contextual
  Accept buttons appear only with `combined_episode` / `suggested_correction`.
- Follow the existing vitest store/component patterns.

## Out of Scope (deferred)

- **Free-text filename editing** — needs a new backend "set filename" endpoint;
  the manual fix in v1 is Rematch (re-pick the title). Revisit only if re-picking
  proves insufficient.
- **Real conflict resolution (Overwrite / Keep both)** — needs new apply options
  server-side; v1 surfaces the warning and resolves via Rematch / Remove / Apply.
- **A browsable phone list** (the A+C hybrid) — jump-to-specific-item was
  explicitly deferred in favor of the focused deck.
- **Redesigning the DV scan panel / Trash / Process menu for mobile** — reused
  as-is in v1.
