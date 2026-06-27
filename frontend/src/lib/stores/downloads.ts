import { writable, derived } from 'svelte/store';
import { connection } from './connection';
import { markDownloaded } from './results';
import { DOWNLOAD_HOSTS } from '$lib/constants';

// Download host preference — persisted to localStorage
const storedHost = typeof localStorage !== 'undefined' ? localStorage.getItem('downloadHost') : null;
export const downloadHost = writable<string>(storedHost || DOWNLOAD_HOSTS[0].value);
downloadHost.subscribe(v => {
  if (typeof localStorage !== 'undefined') localStorage.setItem('downloadHost', v);
});

// Active download progress tracking
export interface DownloadProgress {
  title: string;
  url?: string;
  status: 'resolving' | 'downloading' | 'complete' | 'error';
  linkCount: number;
  method?: string;
}

export const activeDownload = writable<DownloadProgress | null>(null);

export interface BatchProgress {
  completed: number;
  total: number;
  currentTitle: string;
}

export const batchProgress = writable<BatchProgress | null>(null);

let downloadClearTimer: ReturnType<typeof setTimeout> | undefined;
let batchClearTimer: ReturnType<typeof setTimeout> | undefined;

// Download progress events
// Scraping progress events (DDLBase link resolution, shortlink steps)
connection.on('download:resolving', (data) => {
  activeDownload.update((d) => d ? { ...d, status: 'resolving' } : d);
});

connection.on('download:fallback', (data) => {
  // Selenium unavailable — still resolving via fallback
  activeDownload.update((d) => d ? { ...d, status: 'resolving' } : d);
});

connection.on('download:shortlink_step', (data) => {
  activeDownload.update((d) => d ? { ...d, status: 'resolving' } : d);
});

connection.on('download:started', (data) => {
  clearTimeout(downloadClearTimer);
  activeDownload.set({
    title: (data.title as string) || '',
    url: (data.url as string) || '',
    status: 'resolving',
    linkCount: 0,
  });
});

connection.on('download:links_found', (data) => {
  activeDownload.update((d) => d ? { ...d, status: 'downloading', linkCount: (data.link_count as number) || 0 } : d);
});

connection.on('download:complete', (data) => {
  activeDownload.update((d) => d ? { ...d, status: 'complete', method: (data.method as string) || '' } : d);
  // Only a real JDownloader hand-off marks the result row Downloaded —
  // clipboard/browser fallbacks deliver nothing to JD, so they must not.
  const url = (data.url as string) || '';
  if (url && data.method === 'jdownloader') markDownloaded([url]);
  clearTimeout(downloadClearTimer);
  downloadClearTimer = setTimeout(() => activeDownload.set(null), 3000);
});

connection.on('download:failed', (data) => {
  activeDownload.update((d) => d ? { ...d, status: 'error' } : d);
  clearTimeout(downloadClearTimer);
  downloadClearTimer = setTimeout(() => activeDownload.set(null), 4000);
});

connection.on('download:batch_progress', (data) => {
  const completed = (data.completed as number) || 0;
  const total = (data.total as number) || 0;
  batchProgress.set({
    completed,
    total,
    currentTitle: (data.current_title as string) || '',
  });
  if (completed >= total) {
    clearTimeout(batchClearTimer);
    batchClearTimer = setTimeout(() => batchProgress.set(null), 3000);
  }
});

// Bulk "Copy Links" emits download:scrape_progress {completed,total,current}.
// Reuse the same batchProgress bar so both bulk operations share one indicator.
connection.on('download:scrape_progress', (data) => {
  const completed = (data.completed as number) || 0;
  const total = (data.total as number) || 0;
  batchProgress.set({
    completed,
    total,
    currentTitle: (data.current as string) || '',
  });
  if (total > 0 && completed >= total) {
    clearTimeout(batchClearTimer);
    batchClearTimer = setTimeout(() => batchProgress.set(null), 3000);
  }
});

export interface QueueItem {
  id: string;
  title: string;
  url?: string;
  status: 'sending' | 'sent' | 'done' | 'failed';
  addedAt: number;
}

const AUTO_CLEAR_MS = 15_000;

function createDownloadQueue() {
  const { subscribe, update } = writable<QueueItem[]>([]);

  let idCounter = 0;

  function add(title: string, url?: string): string {
    const id = `dq-${++idCounter}-${Date.now()}`;
    update((q) => [{ id, title, url, status: 'sending', addedAt: Date.now() }, ...q]);
    return id;
  }

  // Reconcile a queue item by the url it was created with, as the per-item
  // download:complete / download:failed events arrive.
  function markDoneByUrl(url: string) {
    if (!url) return;
    update((q) => q.map((item) => {
      if (item.url === url && (item.status === 'sending' || item.status === 'sent')) {
        scheduleAutoClear(item.id);
        return { ...item, status: 'done' as const };
      }
      return item;
    }));
  }

  function markFailedByUrl(url: string) {
    if (!url) return;
    update((q) => q.map((item) => {
      if (item.url === url && (item.status === 'sending' || item.status === 'sent')) {
        scheduleAutoClear(item.id);
        return { ...item, status: 'failed' as const };
      }
      return item;
    }));
  }

  function markSent(id: string) {
    update((q) => q.map((item) => (item.id === id ? { ...item, status: 'sent' as const } : item)));
    scheduleAutoClear(id);
  }

  function markDone(id: string) {
    update((q) => q.map((item) => (item.id === id ? { ...item, status: 'done' as const } : item)));
    scheduleAutoClear(id);
  }

  function markFailed(id: string) {
    update((q) =>
      q.map((item) => (item.id === id ? { ...item, status: 'failed' as const } : item))
    );
    scheduleAutoClear(id);
  }

  function remove(id: string) {
    update((q) => q.filter((item) => item.id !== id));
  }

  function clearCompleted() {
    update((q) => q.filter((item) => item.status === 'sending' || item.status === 'sent'));
  }

  function scheduleAutoClear(id: string) {
    setTimeout(() => remove(id), AUTO_CLEAR_MS);
  }

  // Listen for WS notifications about downloads
  connection.on('notification', (data) => {
    const title = (data.title as string) || '';
    const body = (data.body as string) || '';

    if (title === 'Download' || title === 'Batch Download') {
      // Mark matching queue items as done
      update((q) =>
        q.map((item) => {
          if (
            (item.status === 'sending' || item.status === 'sent') &&
            body.toLowerCase().includes(item.title.toLowerCase())
          ) {
            scheduleAutoClear(item.id);
            return { ...item, status: 'done' as const };
          }
          return item;
        })
      );
    } else if (title === 'Download Failed' || title === 'Batch Failed') {
      // Mark sending/sent items as failed
      update((q) =>
        q.map((item) => {
          if (item.status === 'sending' || item.status === 'sent') {
            scheduleAutoClear(item.id);
            return { ...item, status: 'failed' as const };
          }
          return item;
        })
      );
    }
  });

  // Per-item reconciliation from the backend's per-url progress events.
  connection.on('download:complete', (data) => markDoneByUrl((data.url as string) || ''));
  connection.on('download:failed', (data) => markFailedByUrl((data.url as string) || ''));

  return { subscribe, add, markSent, markDone, markFailed, markDoneByUrl, markFailedByUrl, remove, clearCompleted };
}

export const downloadQueue = createDownloadQueue();

// Set of titles currently in-flight (resolving/downloading) or queued (sending/sent).
// Used by result components to overlay a "Downloading" status on the whole title group.
export const downloadingTitles = derived(
  [activeDownload, downloadQueue],
  ([$active, $queue]) => {
    const titles = new Set<string>();
    if ($active && $active.status !== 'complete' && $active.status !== 'error' && $active.title) {
      titles.add($active.title);
    }
    for (const item of $queue) {
      if (item.status === 'sending' || item.status === 'sent') {
        titles.add(item.title);
      }
    }
    return titles;
  }
);
