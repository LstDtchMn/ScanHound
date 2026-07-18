<script lang="ts">
  import { onMount } from 'svelte';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import Tooltip from '$lib/components/Tooltip.svelte';
  import Badge from '$lib/components/Badge.svelte';
  import ConfirmDialog from '$lib/components/ConfirmDialog.svelte';
  import type { TrashEntry } from '$lib/api/types';

  let open = $state(false);
  let loading = $state(false);
  let entries = $state<TrashEntry[]>([]);
  let restoringKey = $state<string | null>(null);
  let deletingKey = $state<string | null>(null);
  // Permanent deletes are irreversible, so both paths route through a
  // ConfirmDialog: `pendingDelete` holds the single entry awaiting
  // confirmation, `confirmEmpty` the empty-everything case.
  let pendingDelete = $state<TrashEntry | null>(null);
  let confirmEmpty = $state(false);
  let emptying = $state(false);

  let totalBytes = $derived(entries.reduce((sum, e) => sum + (e.size ?? 0), 0));
  // failed_db_last_package / db_corruption_flag surfaced from /rename/health —
  // shown as a compact warning badge in the panel header so they're visible
  // without a dedicated health page.
  let dbCorruptionFlag = $state(false);
  let failedDbLastPackage = $state(0);

  function entryKey(e: TrashEntry): string {
    return `${e.bucket}/${e.name}`;
  }

  function formatSize(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let n = bytes / 1024;
    let i = 0;
    while (n >= 1024 && i < units.length - 1) {
      n /= 1024;
      i++;
    }
    return `${n.toFixed(1)} ${units[i]}`;
  }

  async function loadTrash() {
    loading = true;
    try {
      const res = await api.trashList();
      entries = res.entries;
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to load trash', 'error');
    } finally {
      loading = false;
    }
  }

  async function loadHealthBadges() {
    try {
      const health = await api.renameHealth();
      dbCorruptionFlag = !!health.db_corruption_flag;
      failedDbLastPackage = health.failed_db_last_package ?? 0;
    } catch {
      // non-fatal — header badge just stays hidden
    }
  }

  async function restore(entry: TrashEntry) {
    const key = entryKey(entry);
    if (restoringKey) return;
    restoringKey = key;
    try {
      await api.trashRestore(entry.bucket, entry.name);
      entries = entries.filter((e) => entryKey(e) !== key);
      addToast('Trash', `Restored to ${entry.original_path}`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Restore failed', 'error');
    } finally {
      restoringKey = null;
    }
  }

  async function confirmDelete() {
    const entry = pendingDelete;
    if (!entry) return;
    const key = entryKey(entry);
    pendingDelete = null;
    deletingKey = key;
    try {
      const res = await api.trashDelete(entry.bucket, entry.name);
      entries = entries.filter((e) => entryKey(e) !== key);
      addToast('Trash', `Deleted ${entry.name} — freed ${formatSize(res.bytes_freed ?? 0)}`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Delete failed', 'error');
    } finally {
      deletingKey = null;
    }
  }

  async function doEmpty() {
    confirmEmpty = false;
    emptying = true;
    try {
      const res = await api.trashEmpty();
      entries = [];
      addToast('Trash', `Emptied — ${res.files_deleted} file(s), ${formatSize(res.bytes_freed)} freed`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Empty trash failed', 'error');
      // The sweep is fail-safe per file, so a failure may still have removed
      // some entries — reload rather than trusting the stale list.
      loadTrash();
    } finally {
      emptying = false;
    }
  }

  function toggle() {
    open = !open;
    if (open && entries.length === 0 && !loading) {
      loadTrash();
    }
  }

  onMount(() => {
    loadHealthBadges();
  });
</script>

<div class="rounded-lg border border-[var(--border)]">
  <button
    onclick={toggle}
    aria-expanded={open}
    class="w-full flex items-center justify-between px-3 py-2 text-sm font-medium text-left hover:bg-[var(--bg-tertiary)]/40"
  >
    <span class="flex items-center gap-2">
      Trash
      {#if entries.length}
        <Badge label={String(entries.length)} />
      {/if}
      {#if dbCorruptionFlag || failedDbLastPackage > 0}
        <Tooltip text={dbCorruptionFlag
          ? 'A database corruption quarantine flag is currently on disk — check the logs.'
          : `${failedDbLastPackage} file(s) were dropped by the most recent folder import due to a database error.`}>
          <span class="cursor-help">
            <Badge label={dbCorruptionFlag ? 'DB corruption' : `${failedDbLastPackage} DB failure${failedDbLastPackage === 1 ? '' : 's'}`} variant="error" />
          </span>
        </Tooltip>
      {/if}
    </span>
    <span class="text-[var(--text-secondary)] text-xs">{open ? '▴' : '▾'}</span>
  </button>
  {#if open}
    <div class="px-3 pb-3">
      <p class="text-xs text-[var(--text-secondary)] mb-2">
        Files disposed of by a cross-device move land here instead of being permanently deleted
        (unless <em>Require confirmation before permanent deletes</em> is turned off in Settings).
        Swept automatically after the configured retention period.
      </p>
      {#if entries.length}
        <div class="flex items-center justify-between gap-2 mb-2">
          <span class="text-xs text-[var(--text-secondary)]">
            {entries.length} file{entries.length === 1 ? '' : 's'} · {formatSize(totalBytes)}
          </span>
          <button
            onclick={() => (confirmEmpty = true)}
            disabled={emptying}
            class="shrink-0 px-2.5 py-1 rounded-lg border border-[var(--error)] text-[var(--error)] text-xs hover:bg-[var(--error)] hover:text-white disabled:opacity-50 transition-colors"
          >{emptying ? 'Emptying…' : 'Empty trash'}</button>
        </div>
      {/if}
      {#if loading}
        <p class="text-xs text-[var(--text-secondary)]">Loading…</p>
      {:else if entries.length === 0}
        <p class="text-xs text-[var(--text-secondary)]">Trash is empty.</p>
      {:else}
        <div class="rounded-lg border border-[var(--border)] max-h-72 overflow-auto divide-y divide-[var(--border)]">
          {#each entries as entry (entryKey(entry))}
            <div class="px-3 py-1.5 flex items-center gap-2 text-xs">
              <div class="flex-1 min-w-0">
                <div class="truncate font-mono" title={entry.original_path ?? entry.name}>{entry.name}</div>
                <div class="text-[var(--text-secondary)] truncate">
                  {formatSize(entry.size)}
                  {#if entry.trashed_at}· {new Date(entry.trashed_at).toLocaleString()}{/if}
                  {#if entry.original_path}· {entry.original_path}{/if}
                </div>
              </div>
              {#if entry.restorable}
                <button
                  onclick={() => restore(entry)}
                  disabled={restoringKey === entryKey(entry)}
                  class="shrink-0 px-2.5 py-1 rounded-lg border border-[var(--border)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] disabled:opacity-50"
                >{restoringKey === entryKey(entry) ? 'Restoring…' : 'Restore'}</button>
              {:else}
                <Tooltip text="No manifest — restore manually">
                  <button disabled class="shrink-0 px-2.5 py-1 rounded-lg border border-[var(--border)] text-[var(--text-secondary)] opacity-50 cursor-help">Restore</button>
                </Tooltip>
              {/if}
              <button
                onclick={() => (pendingDelete = entry)}
                disabled={deletingKey === entryKey(entry)}
                class="shrink-0 px-2.5 py-1 rounded-lg border border-[var(--border)] text-[var(--error)] hover:bg-[var(--error)] hover:text-white disabled:opacity-50 transition-colors"
              >{deletingKey === entryKey(entry) ? 'Deleting…' : 'Delete'}</button>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>

{#if pendingDelete}
  <ConfirmDialog
    title="Delete permanently?"
    message={`${pendingDelete.name} (${formatSize(pendingDelete.size)}) will be erased from disk. This cannot be undone.`}
    confirmLabel="Delete"
    variant="danger"
    onconfirm={confirmDelete}
    oncancel={() => (pendingDelete = null)}
  />
{/if}

{#if confirmEmpty}
  <ConfirmDialog
    title="Empty trash?"
    message={`All ${entries.length} file${entries.length === 1 ? '' : 's'} (${formatSize(totalBytes)}) will be erased from disk. This cannot be undone.`}
    confirmLabel="Empty trash"
    variant="danger"
    onconfirm={doEmpty}
    oncancel={() => (confirmEmpty = false)}
  />
{/if}
