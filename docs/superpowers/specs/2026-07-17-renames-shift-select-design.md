# Renames list — shift-click range select

**Date:** 2026-07-17 · **Status:** approved (scope: desktop list view only)

## Goal
In the Renames desktop **list** view, let a user select a contiguous range of
rows: click one row's checkbox (the anchor), then shift-click another row's
checkbox to select every row between them, inclusive, in on-screen order.

## Behavior (standard, additive)
- A normal checkbox click toggles that row and sets it as the **anchor**.
- A **shift-click** on another checkbox selects the inclusive range from the
  anchor to the clicked row, in the current visual top-to-bottom order, and
  **adds** it to the existing selection (nothing is deselected). The anchor is
  unchanged, so repeated shift-clicks re-extend from the same anchor.
- Shift-click with no valid anchor (none set, or the anchor scrolled out of the
  current filtered/sorted list) falls back to a normal single toggle + set anchor.
- Order spans the flattened display sequence: standalone rows and the episode
  rows inside season groups, exactly as rendered.

## Scope
- **Desktop list view only.** Grid/tile view (`RenameCard`) and mobile (swipe
  deck) are out — confirmed with the user.
- `RenameRow` is used both standalone and inside `SeasonGroupRow`; changing its
  checkbox handler covers both.

## Design
- **Pure helper** `computeRange(orderedIds, anchorId, targetId): number[]` in a
  new `frontend/src/lib/renames/rangeSelect.ts` — returns the inclusive range
  (handles either direction; returns `[targetId]` if anchor/target missing).
  Unit-tested.
- **Store** (`stores/renames.ts`): add `orderedVisibleIds = writable<number[]>`
  + `setOrderedVisibleIds()`; a module-scoped `selectionAnchorId`; and
  `selectClick(id, shiftKey)` that, on shift with a valid anchor, unions
  `computeRange(get(orderedVisibleIds), anchor, id)` into `selectedJobIds`,
  else toggles `id` and sets the anchor to `id`. `clearSelection()` also resets
  the anchor.
- **Page** (`routes/renames/+page.svelte`): derive the flattened visible id
  order from `groupJobsBySeason(shown)` and push it into the store via
  `$effect`, so nested rows can resolve the range without prop-drilling.
- **Row** (`RenameRow.svelte`): checkbox `onchange` → `onclick` handler that
  calls `selectClick(job.id, e.shiftKey)` and `preventDefault()` (the store +
  `checked={selected}` remain the source of truth).

## Out of scope
Grid/tile and mobile selection; keyboard (ctrl/cmd-click) selection; drag-select.

## Testing
- `rangeSelect.test.ts`: forward range, backward range, anchor==target,
  anchor/target not in list, single-element list.
- `npm run check` + `npm run build` green; manual verify at desktop width.
