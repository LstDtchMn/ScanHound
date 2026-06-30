<script lang="ts">
  import { api } from '$lib/api/client';
  import { folderPreview, loadRenameJobs } from '$lib/stores/renames';
  import { addToast } from '$lib/stores/notifications';
  import type { RenameJob } from '$lib/api/types';

  // Process ▾ split-button: three modes (folder / files / paste-path) all feed the
  // same backend "process-folder" call — recursing a folder, scanning a single
  // folder, or pasting an explicit path. Ported verbatim from the old +page.svelte.
  let open = $state(false);

  // Remember the last folder the user processed instead of hardcoding a
  // host-specific path; empty on first use (the input shows an example).
  let folderPath = $state(
    (typeof localStorage !== 'undefined' && localStorage.getItem('sh-process-folder')) || ''
  );
  let folderBusy = $state(false);
  let previewBusy = $state(false);

  async function processFolder() {
    const folder = folderPath.trim();
    if (!folder || folderBusy) return;
    try { localStorage.setItem('sh-process-folder', folder); } catch {}
    folderBusy = true;
    try {
      await api.renameProcessFolder(folder);
      addToast('Process folder', 'Scanning — rename jobs will appear here as they are identified.');
      open = false;
      // Jobs are created in the background; poll a few times so they show up.
      for (const d of [2000, 5000, 10000]) setTimeout(loadRenameJobs, d);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to start folder processing', 'error');
    } finally {
      folderBusy = false;
    }
  }

  // Dry run: identify + propose targets without creating any jobs or moving
  // files. Result arrives over the WebSocket into the folderPreview store.
  async function previewFolder() {
    const folder = folderPath.trim();
    if (!folder || previewBusy) return;
    try { localStorage.setItem('sh-process-folder', folder); } catch {}
    previewBusy = true;
    folderPreview.set(null);
    try {
      await api.renameProcessFolder(folder, true);
      addToast('Preview', 'Identifying — a preview of what would happen will appear below.');
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to start preview', 'error');
    } finally {
      // The preview itself runs in the background; re-enable shortly after.
      setTimeout(() => (previewBusy = false), 1500);
    }
  }

  // Re-use the old page's preview status pill colours without importing the
  // orchestrator's helper — a tiny local statusClass mirroring it.
  function statusClass(job: RenameJob): string {
    if (job.status === 'failed') return 'bg-[var(--error)]/15 text-[var(--error)]';
    if (job.status === 'needs_review') return 'bg-amber-500/15 text-amber-600 dark:text-amber-400';
    if (job.status === 'applied') return 'bg-[var(--success)]/15 text-[var(--success)]';
    if (job.status === 'reverted') return 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)] line-through';
    return 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]';
  }
</script>

<div class="relative">
  <button
    onclick={() => (open = !open)}
    aria-expanded={open}
    class="text-xs px-2.5 py-1 rounded bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)]"
  >Process ▾</button>

  {#if open}
    <div
      class="absolute right-0 mt-1 z-30 w-[26rem] max-w-[90vw] p-3 rounded-lg bg-[var(--bg-secondary)] border border-[var(--border)] shadow flex flex-col gap-2"
    >
      <div class="flex items-center gap-2 flex-wrap">
        <input
          type="text"
          bind:value={folderPath}
          placeholder="F:\Downloads or a single file path"
          onkeydown={(e) => e.key === 'Enter' && processFolder()}
          class="flex-1 min-w-[12rem] bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-1.5 rounded-lg border border-[var(--border)] text-sm font-mono focus:outline-none focus:border-[var(--accent)]"
        />
        <button
          onclick={previewFolder}
          disabled={previewBusy || !folderPath.trim()}
          title="Identify without creating jobs or moving files"
          class="px-3 py-1.5 text-sm rounded-lg bg-[var(--bg-tertiary)] border border-[var(--border)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)] transition disabled:opacity-50"
        >{previewBusy ? 'Previewing…' : 'Preview'}</button>
        <button
          onclick={processFolder}
          disabled={folderBusy || !folderPath.trim()}
          class="px-3 py-1.5 text-sm rounded-lg bg-[var(--accent)] hover:opacity-90 text-white font-medium transition disabled:opacity-50"
        >{folderBusy ? 'Starting…' : 'Process'}</button>
      </div>
      <p class="text-xs text-[var(--text-secondary)]">
        Scans a folder for video files and creates rename jobs for each — for renaming an existing backlog without JDownloader.
        Paste a single file path to process just that file.
        <strong>Preview</strong> shows what would happen without creating jobs or moving anything.
        Host paths (e.g. <code>F:\Downloads</code>) are translated to the container's mounted view; matches still go through review before moving.
      </p>
      {#if $folderPreview}
        <div class="rounded-lg border border-[var(--border)] overflow-hidden">
          <div class="px-3 py-1.5 bg-[var(--bg-tertiary)] text-xs flex items-center justify-between">
            <span>
              {#if $folderPreview.error}
                <span class="text-[var(--error)]">{$folderPreview.error}</span>
              {:else}
                Preview: <strong>{$folderPreview.would_match ?? 0}</strong> of {$folderPreview.found} file(s) would match
              {/if}
            </span>
            <button onclick={() => folderPreview.set(null)} class="text-[var(--text-secondary)] hover:text-[var(--text-primary)]">Dismiss</button>
          </div>
          {#if $folderPreview.previews?.length}
            <div class="max-h-72 overflow-auto divide-y divide-[var(--border)]">
              {#each $folderPreview.previews as p}
                <div class="px-3 py-1.5 flex items-center gap-2 text-xs">
                  <span class="shrink-0 px-1.5 py-0.5 rounded {statusClass({ status: p.tracked ? 'matched' : p.status } as RenameJob)}">
                    {p.tracked ? 'tracked' : p.status === 'matched' ? `${p.confidence}` : 'review'}
                  </span>
                  <span class="font-mono truncate text-[var(--text-secondary)] flex-1" title={p.filename}>{p.filename}</span>
                  <span class="opacity-60 shrink-0">→</span>
                  <span class="truncate flex-1 {p.new_filename ? '' : 'text-[var(--text-secondary)] italic'}" title={p.new_filename ?? ''}>
                    {p.new_filename ?? (p.title ? `${p.title}${p.year ? ` (${p.year})` : ''}` : 'no match')}
                  </span>
                </div>
              {/each}
            </div>
          {/if}
          {#if $folderPreview.note}
            <p class="px-3 py-1.5 text-[11px] text-[var(--text-secondary)] border-t border-[var(--border)]">{$folderPreview.note}</p>
          {/if}
        </div>
      {/if}
    </div>
  {/if}
</div>
