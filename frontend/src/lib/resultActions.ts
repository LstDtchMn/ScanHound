// Shared action handlers + menu definition for a single ScanResult, consumed by
// both ContextMenu.svelte (desktop right-click) and ResultActionSheet.svelte
// (mobile long-press / "⋯"). Keeps the two touch/mouse surfaces at parity.
import { api } from './api/client';
import { addToast } from './stores/notifications';
import { toggleSelect, markDownloaded } from './stores/results';
import type { ScanResult } from './api/types';

export function downloadResult(item: ScanResult, host: string): void {
  if (!item.url) return;
  // Full metadata so the central downloads row records resolution/season/DV —
  // that row powers duplicate protection and the read-time overlay.
  api.download(item.url, item.title, host, item.year,
               item.resolution || '', item.size || '', item.hdr || '', item.dovi ?? false,
               item.season)
    .catch(() => addToast('Error', 'Download failed', 'error'));
}

export async function copyResultLinks(item: ScanResult, host: string): Promise<void> {
  if (!item.url) return;
  try {
    const { links } = await api.scrapeLinks(item.url, host, item.title, item.resolution);
    if (!links.length) {
      addToast('No Links', `No ${host} links found`, 'warning');
      return;
    }
    await navigator.clipboard.writeText(links.join('\n'));
    markDownloaded([item.url]);
    addToast('Copied', `${links.length} ${host} link(s) copied`);
  } catch (e) {
    addToast('Error', e instanceof Error ? e.message : 'Failed to scrape links', 'error');
  }
}

export function copyResultUrl(item: ScanResult): void {
  if (!item.url) return;
  navigator.clipboard.writeText(item.url).then(
    () => addToast('Copied', 'URL copied'),
    () => addToast('Error', 'Copy failed', 'error')
  );
}

export function openResultImdb(item: ScanResult): void {
  if (item.imdb_id) window.open(`https://www.imdb.com/title/${item.imdb_id}`, '_blank');
}

export function openResultSource(item: ScanResult): void {
  if (item.url) window.open(item.url, '_blank');
}

export function openResultInPlex(item: ScanResult): void {
  api.openInPlex(item.title, item.imdb_id ?? undefined, item.plex_rating_key ?? undefined)
    .catch(() => addToast('Error', 'Failed to open in Plex', 'error'));
}

export function addResultToWatchlist(item: ScanResult): void {
  api.watchlistAdd({ title: item.title, year: item.year, imdb_id: item.imdb_id, item_type: item.season ? 'tv' : 'movie' })
    .then(() => addToast('Watchlist', `Added: ${item.title}`))
    .catch((e) => addToast('Error', e instanceof Error ? e.message : 'Failed to add', 'error'));
}

export interface ResultActionItem {
  key: string;
  label: string;
  icon: string;
  separatorBefore?: boolean;
  run: () => void | Promise<void>;
}

/** Ordered action list for a result item; `host` is the selected download host. */
export function buildResultActions(item: ScanResult, host: string, selected: boolean): ResultActionItem[] {
  const actions: ResultActionItem[] = [
    { key: 'select', label: selected ? 'Deselect' : 'Select', icon: selected ? '☑' : '☐', run: () => toggleSelect(item.url) }
  ];
  if (item.url) {
    actions.push({ key: 'download', label: `Download (${host})`, icon: '⬇', run: () => downloadResult(item, host) });
    actions.push({ key: 'copyLinks', label: 'Copy links', icon: '🔗', run: () => copyResultLinks(item, host) });
    actions.push({ key: 'copyUrl', label: 'Copy URL', icon: '📋', run: () => copyResultUrl(item) });
    actions.push({ key: 'openSource', label: 'Open source page', icon: '↗', run: () => openResultSource(item) });
  }
  if (item.imdb_id) {
    actions.push({ key: 'openImdb', label: 'Open on IMDb', icon: 'IMDb', separatorBefore: true, run: () => openResultImdb(item) });
  }
  if (item.plex_rating_key) {
    actions.push({ key: 'openInPlex', label: 'Open in Plex', icon: 'Plex', run: () => openResultInPlex(item) });
  }
  actions.push({ key: 'addToWatchlist', label: 'Add to watchlist', icon: '＋', separatorBefore: true, run: () => addResultToWatchlist(item) });
  return actions;
}
