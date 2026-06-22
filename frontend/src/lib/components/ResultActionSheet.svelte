<script lang="ts">
  import BottomSheet from './BottomSheet.svelte';
  import { toggleSelect, selectedKeys } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { api } from '$lib/api/client';
  import { addToast } from '$lib/stores/notifications';
  import type { ScanResult } from '$lib/api/types';

  interface Props {
    item: ScanResult | null;
    onclose: () => void;
  }
  let { item, onclose }: Props = $props();

  let selected = $derived(!!item && $selectedKeys.has(item.url));

  function run(fn: () => void) {
    fn();
    onclose();
  }

  function download() {
    if (item?.url) api.download(item.url, item.title, $downloadHost, item.year).catch(() => addToast('Error', 'Download failed', 'error'));
  }
  async function copyLinks() {
    if (!item?.url) return;
    try {
      const { links } = await api.scrapeLinks(item.url, $downloadHost, item.title, item.resolution);
      if (!links.length) { addToast('No Links', `No ${$downloadHost} links found`, 'warning'); return; }
      await navigator.clipboard.writeText(links.join('\n'));
      addToast('Copied', `${links.length} ${$downloadHost} link(s) copied`);
    } catch (e) {
      addToast('Error', e instanceof Error ? e.message : 'Failed to scrape links', 'error');
    }
  }
  function copyUrl() {
    if (item?.url) navigator.clipboard.writeText(item.url).then(() => addToast('Copied', 'URL copied'), () => addToast('Error', 'Copy failed', 'error'));
  }
  function openImdb() {
    if (item?.imdb_id) window.open(`https://www.imdb.com/title/${item.imdb_id}`, '_blank');
  }
  function openSource() {
    if (item?.url) window.open(item.url, '_blank');
  }
  function openInPlex() {
    if (item) api.openInPlex(item.title, item.imdb_id ?? undefined, item.plex_rating_key ?? undefined).catch(() => addToast('Error', 'Failed to open in Plex', 'error'));
  }
  function addToWatchlist() {
    if (!item) return;
    api.watchlistAdd({ title: item.title, year: item.year, imdb_id: item.imdb_id, item_type: item.season ? 'tv' : 'movie' })
      .then(() => addToast('Watchlist', `Added: ${item.title}`))
      .catch((e) => addToast('Error', e instanceof Error ? e.message : 'Failed to add', 'error'));
  }

  const rowClass = 'w-full text-left px-3 py-3 rounded-lg text-sm text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] active:bg-[var(--bg-tertiary)] transition-colors flex items-center gap-3';
</script>

<BottomSheet open={!!item} title={item?.title} onclose={onclose}>
  {#if item}
    <div class="space-y-1">
      <button class={rowClass} onclick={() => run(() => toggleSelect(item.url))}>
        <span class="w-5 text-center">{selected ? '☑' : '☐'}</span>{selected ? 'Deselect' : 'Select'}
      </button>
      {#if item.url}
        <button class={rowClass} onclick={() => run(download)}><span class="w-5 text-center">⬇</span>Download ({$downloadHost})</button>
        <button class={rowClass} onclick={() => run(copyLinks)}><span class="w-5 text-center">🔗</span>Copy links</button>
        <button class={rowClass} onclick={() => run(copyUrl)}><span class="w-5 text-center">📋</span>Copy URL</button>
        <button class={rowClass} onclick={() => run(openSource)}><span class="w-5 text-center">↗</span>Open source page</button>
      {/if}
      {#if item.imdb_id}
        <button class={rowClass} onclick={() => run(openImdb)}><span class="w-5 text-center font-bold text-[10px]">IMDb</span>Open on IMDb</button>
      {/if}
      {#if item.plex_rating_key}
        <button class={rowClass} onclick={() => run(openInPlex)}><span class="w-5 text-center font-bold text-[10px]">Plex</span>Open in Plex</button>
      {/if}
      <button class={rowClass} onclick={() => run(addToWatchlist)}><span class="w-5 text-center">＋</span>Add to watchlist</button>
    </div>
  {/if}
</BottomSheet>
