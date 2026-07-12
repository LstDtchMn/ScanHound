import { writable, derived, get } from 'svelte/store';
import { api } from '$lib/api/client';
import { connection } from './connection';
import type { RenameJob, RenameStatus, DvScan } from '$lib/api/types';
import { persisted } from '$lib/stores/results';
import { addToast } from '$lib/stores/notifications';
import type { RenameCategory } from '$lib/renames/category';

export const renameJobs = writable<RenameJob[]>([]);
export const renameStatus = writable<RenameStatus | null>(null);

/** Archived rename jobs — a SEPARATE store from renameJobs, since the
 *  default (non-archived) load never includes them. Fetched fresh each
 *  time the Archived tab is selected, not filtered client-side from
 *  renameJobs. */
export const archivedRenameJobs = writable<RenameJob[]>([]);

export async function loadArchivedRenameJobs() {
  try {
    const { jobs } = await api.getRenameJobs(undefined, true);
    archivedRenameJobs.set(jobs);
  } catch {
    /* offline / no server */
  }
}

// --- Multi-select ---
export const selectedJobIds = writable<Set<number>>(new Set());
export function toggleSelect(id: number) {
  selectedJobIds.update((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });
}
export function selectAll(ids: number[]) {
  selectedJobIds.set(new Set(ids));
}
export function clearSelection() {
  selectedJobIds.set(new Set());
}

// --- View / sort / category / search prefs ---
export const viewMode = persisted<'list' | 'grid'>('sh-renames-view', 'list');
export const renameSort = persisted<
  'detected_desc' | 'detected_asc' | 'confidence_desc' | 'title_asc'
>('sh-renames-sort', 'detected_desc');
export const renameCategory = persisted<RenameCategory>('sh-renames-category', 'all');
export const renameQuery = writable<string>('');

// --- Bulk in-flight flag (disables BulkBar during a run) ---
export const bulkBusy = writable<boolean>(false);

export interface FolderPreviewItem {
  path: string;
  filename: string;
  tracked: boolean;
  title: string | null;
  year: number | null;
  confidence: number;
  new_filename: string | null;
  status: string;
}
export interface FolderPreview {
  folder: string;
  found: number;
  would_match?: number;
  previews?: FolderPreviewItem[];
  note?: string;
  error?: string;
}
/** Latest dry-run preview from "Process folder → Preview" (no jobs created). */
export const folderPreview = writable<FolderPreview | null>(null);

/** Count of jobs flagged for manual review — surfaced as a nav badge. */
export const needsReviewCount = derived(renameStatus, ($s) => $s?.needs_review ?? 0);

export async function loadRenameJobs(status?: string) {
  try {
    const { jobs } = await api.getRenameJobs(status);
    renameJobs.set(jobs);
  } catch {
    /* offline / no server */
  }
}

export async function loadRenameStatus() {
  try {
    renameStatus.set(await api.getRenameStatus());
  } catch {
    /* offline */
  }
}

async function refresh() {
  await Promise.all([loadRenameJobs(), loadRenameStatus()]);
}

export async function refreshRenames() {
  await refresh();
}

export async function applyJob(id: number, strategy?: 'overwrite' | 'keep_both' | 'skip') {
  await api.applyRename(id, strategy ? { conflict_strategy: strategy } : undefined);
  await refresh();
}

export async function undoJob(id: number) {
  const r = await api.undoRename(id);
  if (r.restore_warning) {
    addToast('Undo', r.restore_warning, 'warning');
  }
  await refresh();
}

export async function rematchJob(id: number, tmdbId: number, mediaType?: string) {
  await api.rematchRename(id, tmdbId, mediaType);
  await refresh();
}

// --- Bulk actions ---
async function runBulk(label: string, fn: (ids: number[]) => Promise<void>) {
  const ids = [...get(selectedJobIds)];
  if (ids.length === 0) return;
  bulkBusy.set(true);
  try {
    await fn(ids);
  } catch (e) {
    addToast(`${label} failed`, e instanceof Error ? e.message : String(e), 'error');
  } finally {
    await refresh();
    clearSelection();
    bulkBusy.set(false);
  }
}

export function bulkApply() {
  return runBulk('Apply', async (ids) => {
    // Applies are queued server-side (cross-device moves can take minutes);
    // each job reports back over the rename:job WS event as it lands.
    const r = await api.bulkApply(ids);
    if (r.busy) {
      // The apply-triggering controls are disabled while $applyActive is
      // true, so this should be unreachable from a single tab — but stays
      // as a clear fallback (e.g. a second tab) rather than a confusing
      // "Applying 0 in background" toast.
      addToast('Apply already running', 'Another apply is in progress — try again once it finishes.', 'warning');
      return;
    }
    addToast(
      `Applying ${r.queued ?? 0} in background`,
      r.skipped ? `${r.skipped} skipped (already applied/in progress)` : 'Progress updates live',
      'info'
    );
  });
}

export function bulkReidentify() {
  return runBulk('Re-identify', async (ids) => {
    const r = await api.bulkReidentify(ids);
    addToast('Re-identify queued', `Queued ${r.queued} for re-identify`, 'info');
  });
}

export function bulkDelete() {
  return runBulk('Delete', async (ids) => {
    const r = await api.bulkDelete(ids);
    addToast('Deleted', `Deleted ${r.deleted}`, 'success');
  });
}

export function bulkArchive() {
  return runBulk('Archive', async (ids) => {
    const r = await api.bulkArchive(ids);
    addToast('Archived', `Archived ${r.archived} job(s).`, 'success');
  });
}

export function bulkUnarchive() {
  return runBulk('Unarchive', async (ids) => {
    const r = await api.bulkUnarchive(ids);
    addToast('Restored', `Restored ${r.unarchived} job(s) to the queue.`, 'success');
    // runBulk's own finally block calls refresh(), which only reloads
    // renameJobs/renameStatus — not this separate archivedRenameJobs store.
    // Unarchiving must also refresh the Archived tab the user is currently
    // viewing, or the just-restored jobs would appear to linger there until
    // the next manual reload.
    await loadArchivedRenameJobs();
  });
}

export function bulkSetDestination(root: string) {
  return runBulk('Set destination', async (ids) => {
    const r = await api.bulkSetDestination(ids, root);
    addToast('Destination updated', `Updated destination for ${r.updated}`, 'success');
  });
}

/** Apply-confident. ids omitted = all matched jobs (Matched-card shortcut). */
export async function applyConfident(ids?: number[]) {
  bulkBusy.set(true);
  try {
    const r = await api.applyConfident(ids);
    if (r.busy) {
      // Same fallback as bulkApply() above — the "Apply all confident"
      // control is disabled while $applyActive is true, so this is
      // normally unreachable from a single tab.
      addToast('Apply already running', 'Another apply is in progress — try again once it finishes.', 'warning');
      return;
    }
    addToast(
      `Applying ${r.queued ?? 0} confident in background`,
      `${r.skipped ?? 0} skipped — progress updates live`,
      'info'
    );
  } catch (e) {
    addToast(
      'Apply confident failed',
      e instanceof Error ? e.message : String(e),
      'error'
    );
  } finally {
    await refresh();
    clearSelection();
    bulkBusy.set(false);
  }
}

/** "Stop applying" — gracefully halts the running apply queue after its
 *  in-flight file finishes. Reverted jobs reappear as Matched via the
 *  normal rename:job broadcast; no local state to reconcile here. */
export async function cancelApply() {
  applyCancelling.set(true);
  try {
    await api.cancelApply();
  } catch (e) {
    applyCancelling.set(false);
    addToast('Stop applying failed', e instanceof Error ? e.message : String(e), 'error');
  }
}

export async function acceptCombinedJob(id: number) {
  await api.acceptCombinedRename(id);
  await refresh();
}

export async function acceptCorrectionJob(id: number) {
  await api.acceptCorrectionRename(id);
  await refresh();
}

export async function deleteJob(id: number) {
  await api.deleteRenameJob(id);
  renameJobs.update((jobs) => jobs.filter((j) => j.id !== id));
  await loadRenameStatus();
}

// ── Apply progress ───────────────────────────────────────────────────
// Per-job byte progress for an in-flight move (only a cross-device COPY emits
// this; same-device renames finish instantly). Keyed by job id.
export interface RenameProgress {
  pct: number;
  bytes_done: number;
  bytes_total: number;
  /** EMA-smoothed transfer rate from the server, or null until the first
   *  windowed sample exists (see TransferRateEstimator, backend/rename/service.py). */
  bytes_per_sec: number | null;
  /** Derived from the same EMA rate; null once pct reaches 100 (nothing left
   *  to wait for — the post-copy hash-verify has no byte-level progress). */
  eta_seconds: number | null;
  /** Client-side receipt time (Date.now()), NOT from the server — drives the
   *  "stalled" (no update for a while) heuristic below. */
  updatedAt: number;
}
export const renameProgress = writable<Map<number, RenameProgress>>(new Map());

/** Ticks once a second so components deriving off `renameProgress[id].updatedAt`
 *  (i.e. "has this job gone quiet?") re-evaluate without needing their own
 *  interval. Started lazily so importing this module in a non-browser test
 *  environment (jsdom/vitest, SSR) never leaves a live timer running. */
export const progressClock = writable<number>(Date.now());
if (typeof window !== 'undefined') {
  setInterval(() => progressClock.set(Date.now()), 1000);
}

// Overall apply-queue progress ("job X of N"). null when nothing is applying.
export interface RenameQueueProgress { done: number; total: number; current_title: string | null; }
export const renameQueue = writable<RenameQueueProgress | null>(null);

// True whenever a bulk apply run is active (renameQueue is non-null, including
// the brief post-completion "100%" flash — see the rename:queue_progress
// handler below). Drives the disabled state of every apply-triggering control
// (BulkBar's Apply/Apply confident/Set destination/Re-identify/Delete,
// StatusDashboard's "Apply all confident" link) so a second bulk-apply run can
// never be started from the UI while one is already in flight. The backend
// (RenameService.queue_apply) is the real, authoritative guard against
// overlap — this is purely a UX nicety to keep the buttons from firing a
// request that the server would just reject as busy.
export const applyActive = derived(renameQueue, ($q) => $q !== null);

// True from a "Stop applying" click until the queue actually clears — drives
// the Stop button's disabled/"Stopping…" state. The cancel POST returns
// immediately; the queue keeps running until its in-flight file finishes, so
// this is reset the moment the queue's completion broadcast (active:false)
// arrives — see the rename:queue_progress handler below. It does not wait on
// the queueClearTimer's 1500ms "100%" flash, so the Stop button (and the
// apply-triggering controls gated on applyActive above) can't be left stuck
// disabled/"Stopping…" any longer than the run actually takes.
export const applyCancelling = writable<boolean>(false);

connection.on('rename:progress', (data) => {
  const id = data.id as number;
  if (!id) return;
  // A late/reordered tick for a job that's already left 'applying' (e.g. its
  // terminal rename:job broadcast already arrived and cleared this map) must
  // not resurrect a stale bar — broadcast_sync (backend/api/ws.py) has no
  // per-connection send serialization, so two back-to-back broadcasts (a
  // final progress tick, then the terminal status change) have no hard
  // wire-order guarantee once they're actually being written to the socket.
  // Trust the job's last-known status (kept current by the rename:job
  // handler below) over tick arrival order. A job not yet known locally
  // (e.g. its very first tick arrives before any rename:job snapshot) is
  // let through — there's nothing to contradict it yet.
  const knownJob = get(renameJobs).find((j) => j.id === id);
  if (knownJob && knownJob.status !== 'applying') return;
  renameProgress.update((m) => {
    const next = new Map(m);
    next.set(id, {
      pct: (data.pct as number) ?? 0,
      bytes_done: (data.bytes_done as number) ?? 0,
      bytes_total: (data.bytes_total as number) ?? 0,
      bytes_per_sec: (data.bytes_per_sec as number | null) ?? null,
      eta_seconds: (data.eta_seconds as number | null) ?? null,
      updatedAt: Date.now(),
    });
    return next;
  });
});

let queueClearTimer: ReturnType<typeof setTimeout>;
connection.on('rename:queue_progress', (data) => {
  const active = !!data.active;
  clearTimeout(queueClearTimer);
  if (!active) {
    // The run is over (normal completion or a cancel) — reset the Stop
    // button/apply-triggering-controls gate immediately rather than waiting
    // on the cosmetic flash timer below. The backend now allows only one
    // bulk-apply run at a time (RenameService.queue_apply's _bulk_lock
    // guard), so there's no second run left that could still need
    // cancelling by the time this broadcast arrives.
    applyCancelling.set(false);
    // Brief "100%" flash, then clear the queue bar.
    renameQueue.set({ done: (data.done as number) ?? 0, total: (data.total as number) ?? 0, current_title: null });
    queueClearTimer = setTimeout(() => {
      renameQueue.set(null);
    }, 1500);
    return;
  }
  renameQueue.set({
    done: (data.done as number) ?? 0,
    total: (data.total as number) ?? 0,
    current_title: (data.current_title as string) ?? null,
  });
});

// Live updates: upsert a job whenever the backend broadcasts a change.
connection.on('rename:job', (data) => {
  const job = data as unknown as RenameJob;
  if (!job || !job.id) return;
  if (job.archived_at) {
    // Auto-archive-on-apply (backend/rename/service.py's two apply() success
    // paths set archived_at in the same update_rename_job() call that sets
    // status="applied") lands here, not via a separate event — this is the
    // ONLY signal a live session gets that a job just left the active queue.
    // renameJobs models the non-archived queue (loadRenameJobs's default
    // archived=false), so an archived job must be evicted, not upserted, or
    // it visibly lingers in the active list until a full page reload.
    renameJobs.update((jobs) => jobs.filter((j) => j.id !== job.id));
    renameProgress.update((m) => {
      if (!m.has(job.id)) return m;
      const next = new Map(m);
      next.delete(job.id);
      return next;
    });
    // Keep an open Archived tab live too -- a job can be updated (e.g.
    // rematched) while already archived; upsert here so the row doesn't
    // show stale data until the tab is left and re-entered.
    archivedRenameJobs.update((jobs) => {
      const idx = jobs.findIndex((j) => j.id === job.id);
      if (idx >= 0) {
        const next = [...jobs];
        next[idx] = job;
        return next;
      }
      return [job, ...jobs];
    });
    loadRenameStatus();
    return;
  }
  renameJobs.update((jobs) => {
    const idx = jobs.findIndex((j) => j.id === job.id);
    if (idx >= 0) {
      const next = [...jobs];
      next[idx] = job;
      return next;
    }
    return [job, ...jobs];
  });
  // Symmetric case: a job that WAS archived and no longer is (e.g.
  // unarchived) must leave the Archived view too, or a stale copy lingers.
  archivedRenameJobs.update((jobs) => jobs.filter((j) => j.id !== job.id));
  // Once a job leaves 'applying' (applied/failed/needs_review), drop its
  // per-item progress so the bar disappears.
  if (job.status !== 'applying') {
    renameProgress.update((m) => {
      if (!m.has(job.id)) return m;
      const next = new Map(m);
      next.delete(job.id);
      return next;
    });
  }
  loadRenameStatus();
});

// ── Reconnect resync ────────────────────────────────────────────────
// A WS disconnect (laptop sleep, network blip — connection.ts has no
// heartbeat, so a half-dead socket can sit undetected for a while before
// onclose fires) can silently swallow a job's terminal rename:job broadcast
// or the apply-queue's terminal rename:queue_progress(active:false)
// broadcast: broadcast_sync (backend/api/ws.py) is fire-and-forget with no
// redelivery, so whatever this client missed while disconnected is gone for
// good. Left uncorrected, a completed job's row stays frozen on a stale
// 'applying' status / progress bar forever — nothing will ever emit another
// event for a job that's already finished, so nothing would ever correct it
// short of a hard page reload (which re-mounts and re-fetches from scratch).
//
// On reconnect, trust a fresh GET over any locally-held progress state — but
// the *general* jobs page (GET /rename/jobs, no status filter) is capped
// (backend default limit=200) and ordered by `detected_at DESC` — a job's
// original detection time, not its apply time. A job that sat in a large
// needs_review/matched backlog before finally being applied can be
// genuinely still mid-copy yet absent from that page entirely once a
// background scanner has detected 200+ newer items since. Treating "absent
// from the general page" as "confirmed not applying" would incorrectly wipe
// that *live* job's progress and let its row vanish from the UI until its
// next broadcast. Query the `status=applying` filter specifically as the
// authoritative source for "which jobs are actually still applying" — the
// backend serializes bulk/single applies through one `_bulk_lock`
// (RenameService), so this is at most a handful of rows regardless of
// backlog depth — and use it both to restore/keep that job's row and to
// decide which renameProgress entries survive.
const RESYNC_RETRY_DELAY_MS = 2000;

interface ResyncSnapshot {
  jobs: RenameJob[];
  status: RenameStatus;
  applyingJobs: RenameJob[];
}

async function fetchResyncSnapshot(): Promise<ResyncSnapshot | null> {
  try {
    const [{ jobs }, status, { jobs: applyingJobs }] = await Promise.all([
      api.getRenameJobs(),
      api.getRenameStatus(),
      api.getRenameJobs('applying'),
    ]);
    return { jobs, status, applyingJobs };
  } catch {
    return null;
  }
}

export async function resyncAfterReconnect() {
  let snapshot = await fetchResyncSnapshot();
  if (!snapshot) {
    // A transient blip right at reconnect (e.g. a backend restart where the
    // WS upgrade path and the plain HTTP routes behind a reverse proxy don't
    // become available in perfect lockstep) shouldn't leave the exact stale
    // state this function exists to correct — retry once before giving up.
    await new Promise((r) => setTimeout(r, RESYNC_RETRY_DELAY_MS));
    snapshot = await fetchResyncSnapshot();
  }
  if (!snapshot) {
    // Both attempts failed — surface this rather than silently no-op'ing on
    // exactly the state this function exists to fix. Local state (including
    // any stale progress) is left untouched; the next reconnect (or a
    // manual refresh) gets another chance.
    console.warn('resyncAfterReconnect: REST refresh failed after reconnect + 1 retry');
    addToast(
      'Reconnected, but refresh failed',
      'Move progress may be stale until the next successful refresh.',
      'warning'
    );
    return;
  }
  // Merge: start from the general page, then make sure any still-applying
  // job it missed (see comment above) isn't dropped from view.
  const byId = new Map(snapshot.jobs.map((j) => [j.id, j] as const));
  for (const j of snapshot.applyingJobs) byId.set(j.id, j);
  // The three REST reads above are independent queries with no shared
  // transaction, and the live WS socket is already open and dispatching
  // during this whole async round-trip (reconnectHandlers fire in
  // ws.onopen, before this function's fetches resolve). So a job can go
  // 'applying' -> 'applied' entirely between two of these reads, or a
  // terminal rename:job broadcast can land — and be applied by the handler
  // above — while this fetch is still in flight. Either way this snapshot
  // is then stale for that job, and blindly trusting it here would clobber
  // the fresher, already-correct local state back to 'applying' — the
  // exact bug this resync exists to fix, reintroduced as a race. Only a WS
  // broadcast ever sets a terminal status (applied/failed), so a locally-
  // held terminal status is always at least as fresh as anything these
  // fetches could have read; never let this snapshot regress one.
  const TERMINAL = new Set(['applied', 'failed']);
  for (const local of get(renameJobs)) {
    if (!TERMINAL.has(local.status)) continue;
    // A locally-known-archived job must never be replanted into the active
    // queue just because these archived-excluding snapshot reads don't
    // include it — that would defeat the whole point of archiving it. (In
    // practice the live rename:job handler above already evicts an archived
    // job from renameJobs the instant it learns archived_at, so this is
    // belt-and-suspenders — but it's the exact scenario a prior review
    // flagged this merge as blind to, so keep the guard explicit.)
    if (local.archived_at) continue;
    const merged = byId.get(local.id);
    if (!merged || !TERMINAL.has(merged.status)) byId.set(local.id, local);
  }
  renameJobs.set([...byId.values()]);
  renameStatus.set(snapshot.status);
  const applyingIds = new Set(snapshot.applyingJobs.map((j) => j.id));
  renameProgress.update((m) => {
    if (m.size === 0) return m;
    const next = new Map(m);
    for (const id of next.keys()) {
      if (!applyingIds.has(id)) next.delete(id);
    }
    return next;
  });
  // Only clear the queue banner (and thus applyActive) when the snapshot's
  // own authoritative applying-filter confirms nothing is actually still
  // running — it was fetched moments ago in this same round-trip.
  // Unconditionally nulling it, even while a bulk run is genuinely mid-copy
  // between per-job queue_progress broadcasts (which for one large file can
  // be minutes apart), would hide the Stop button and re-enable every
  // applyActive-gated control for however long remains until the next one.
  if (snapshot.applyingJobs.length === 0) {
    renameQueue.set(null);
    applyCancelling.set(false);
  }
}

connection.onReconnect(() => {
  resyncAfterReconnect();
});

// Dry-run preview result (no jobs are created for a preview).
connection.on('rename:folder_preview', (data) => {
  folderPreview.set(data as unknown as FolderPreview);
});

// ── Dolby Vision scan ────────────────────────────────────────────────
export interface DvScanProgress { done: number; total: number; file: string; layer: string | null; }
export interface DvScanResult {
  folder?: string; found: number; scanned: number; skipped: number;
  by_layer?: Record<string, number>; error?: string;
}
/** True from the moment a DV scan is dispatched until its dv:scan_done arrives —
 *  drives the Scan button's disabled state (the POST returns immediately, so a
 *  timer can't tell when the background scan actually finishes). */
export const dvScanRunning = writable<boolean>(false);
/** Live per-file progress of a running DV scan (null when idle). */
export const dvScanProgress = writable<DvScanProgress | null>(null);
/** Summary of the last completed DV scan. */
export const dvScanResult = writable<DvScanResult | null>(null);
/** The DV inventory (scanned files) + per-layer counts. */
export const dvScans = writable<DvScan[]>([]);
export const dvCounts = writable<Record<string, number>>({});

export async function loadDvScans(layer?: string) {
  try {
    const { scans, counts } = await api.getDvScans(layer);
    dvScans.set(scans);
    dvCounts.set(counts);
  } catch {
    /* offline */
  }
}

connection.on('dv:scan_progress', (data) => {
  dvScanProgress.set(data as unknown as DvScanProgress);
});
connection.on('dv:scan_done', (data) => {
  dvScanResult.set(data as unknown as DvScanResult);
  dvScanProgress.set(null);
  dvScanRunning.set(false);
  loadDvScans();
});

// ── On-demand conflict DV scan (RenameReviewCard's "Scan DV layers") ──
// Distinct from the full-library scan above: scan-dv-conflict broadcasts its
// own dv:conflict_scan_done so it never disturbs the DV-scan panel's state.
// Consumers just watch this tick to know when to re-fetch their conflict
// preview — no shared progress/result payload to model.
export const dvScanTick = writable(0);
connection.on('dv:conflict_scan_done', () => {
  dvScanTick.update((n) => n + 1);
});

// ── Dolby Vision label sync (mirrors the DV scan stores above) ────────
export interface DvSyncProgress { done: number; total: number; }
export interface DvSyncResult {
  total: number; added: number; removed: number; matched: number; dry_run: boolean;
  error?: string;
}
/** True from dispatch of a label sync until its dv:sync_done arrives —
 *  drives the "Sync Plex labels" button's disabled state. */
export const dvSyncRunning = writable<boolean>(false);
/** Live done/total progress of a running label sync (null when idle) — the
 *  backend reports counts only, no per-title text (see dv_sync_labels in
 *  backend/api/routes/rename.py). */
export const dvSyncProgress = writable<DvSyncProgress | null>(null);
/** Summary of the last completed label sync. */
export const dvSyncResult = writable<DvSyncResult | null>(null);

connection.on('dv:sync_progress', (data) => {
  dvSyncProgress.set(data as unknown as DvSyncProgress);
});
connection.on('dv:sync_done', (data) => {
  dvSyncResult.set(data as unknown as DvSyncResult);
  dvSyncProgress.set(null);
  dvSyncRunning.set(false);
});
