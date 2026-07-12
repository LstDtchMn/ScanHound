<script lang="ts">
  import RenameRow from './RenameRow.svelte';
  import { seasonSummary } from '$lib/renames/seasonGroups';
  import { applyConfident, bulkBusy, applyActive } from '$lib/stores/renames';
  import type { RenameJob } from '$lib/api/types';

  let { jobs, show, season, onRematch, onCompare }: {
    jobs: RenameJob[];
    show: string;
    season: number;
    onRematch: (job: RenameJob) => void;
    onCompare: (job: RenameJob) => void;
  } = $props();

  let expanded = $state(false);
  let summary = $derived(seasonSummary(jobs));
  // `jobs` here is always homogenous in archived-ness -- +page.svelte's
  // `shown` sources exclusively from either $archivedRenameJobs or
  // $renameJobs, never a mix -- but check per-job defensively rather than
  // lean on that invariant: an archived season (all its jobs archived_at
  // set) must not offer a live "Apply all" that bypasses the Archived tab's
  // read-only-until-Unarchive design.
  let allArchived = $derived(jobs.length > 0 && jobs.every((j) => j.archived_at));
  let applyDisabled = $derived($bulkBusy || $applyActive || summary.matched === 0 || allArchived);

  function applyAll() {
    if (applyDisabled) return;
    const ids = jobs.filter((j) => j.status === 'matched' && !j.archived_at).map((j) => j.id);
    if (ids.length > 0) applyConfident(ids);
  }
</script>

<!--
  Root is <li>, not <div>: this component is rendered as a direct child of
  the page's <ul> (list view), and a non-<li> child there is invalid — see
  the same concern already called out for modals in +page.svelte. The
  expanded body below is a nested <ul> (valid inside an <li>) so each
  episode's RenameRow (itself an <li>) stays correctly nested rather than
  landing as a stray <li> inside a <div>.

  The header is a <div> flex row containing two SIBLING <button>s (toggle +
  Apply all), not one <button> nested inside another: HTML's parser closes
  an outer <button> as soon as it sees a nested <button> start tag, which
  would silently kick "Apply all" out of the flex row at runtime.
-->
<li class="bg-[var(--bg-primary)]">
  <div class="w-full flex items-center gap-2 px-3 py-2 bg-[var(--bg-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors">
    <button
      type="button"
      class="flex-1 min-w-0 flex items-center gap-2 text-left"
      onclick={() => (expanded = !expanded)}
      aria-expanded={expanded}
    >
      <span class="text-xs shrink-0">{expanded ? '▾' : '▸'}</span>
      <span class="text-sm font-medium text-[var(--text-primary)] truncate">{show} &mdash; Season {season}</span>
      <span class="text-xs text-[var(--text-secondary)] truncate">
        {summary.matched} matched
        {#if summary.needsReview > 0} &middot; {summary.needsReview} needs review{/if}
        {#if summary.conflicts > 0} &middot; {summary.conflicts} conflict{summary.conflicts === 1 ? '' : 's'}{/if}
        {#if summary.applied > 0} &middot; {summary.applied} applied{/if}
      </span>
    </button>
    <button
      type="button"
      class="shrink-0 px-2 py-1 rounded text-[11px] font-medium bg-[var(--accent)] text-white disabled:opacity-50"
      disabled={applyDisabled}
      onclick={applyAll}
    >Apply all</button>
  </div>
  {#if expanded}
    <ul class="divide-y divide-[var(--border)]">
      {#each jobs as job (job.id)}
        <RenameRow {job} {onRematch} {onCompare} />
      {/each}
    </ul>
  {/if}
</li>
