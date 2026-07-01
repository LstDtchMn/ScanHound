import type { ResultsResponse, CachedResultsResponse, BackgroundStatus, RenameJob, RenameStatus, RenameStats, DvScan, PlexStatus, AnalyticsSummary, LibraryStats, TrendData, WatchlistItem, WatchlistStats, WatchlistExport, Settings, JdStatus, JdRunState, DownloadResult, DownloadHistoryEntry, BulkApplyResponse, BulkReidentifyResponse, BulkDeleteResponse, BulkSetDestResponse, ApplyConfidentResponse, TmdbSearchResult, RematchPreviewResponse, RematchConfirmResponse } from './types';
import { apiBase, getStoredToken } from './endpoint';

const REQUEST_TIMEOUT_MS = 15_000;

/** Auth token: a stored token (Android/remote) seeds it; the Tauri sidecar or
 *  setAuthNonce() can override at runtime. Empty = dev mode (no auth). */
let authNonce = getStoredToken();

export function setAuthNonce(nonce: string) {
  authNonce = nonce;
}

export function getAuthNonce(): string {
  return authNonce;
}

/** Invoked when an API call returns 401 (token missing/expired). The root
 *  layout registers a handler that redirects to /login. Not fired for the
 *  /auth/* endpoints, which surface their own errors (e.g. a wrong password). */
let unauthorizedHandler: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null) {
  unauthorizedHandler = fn;
}

/** fetch() with an abort-based timeout, shared by this client and anything
 *  else (e.g. the remote-server connection test) that needs to probe a URL
 *  the way request() does without going through apiBase()/authNonce. */
export async function fetchWithTimeout(
  url: string,
  options: RequestInit,
  timeoutMs: number
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      throw new Error(`Request timed out after ${timeoutMs / 1000}s`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers = new Headers(options?.headers);
  if (options?.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (authNonce) {
    headers.set('Authorization', `Bearer ${authNonce}`);
  }

  const resp = await fetchWithTimeout(`${apiBase()}${path}`, { ...options, headers }, REQUEST_TIMEOUT_MS);
  if (!resp.ok) {
    if (resp.status === 401 && !path.startsWith('/auth/')) {
      unauthorizedHandler?.();
    }
    throw new Error(`API error: ${resp.status} ${resp.statusText}`);
  }
  const ct = resp.headers.get('content-type') || '';
  if (!ct.includes('application/json')) {
    throw new Error(`Unexpected content type: ${ct}`);
  }
  const data = await resp.json();
  // Defensive fallback: catch error payloads that slipped through with 200
  if (data && typeof data === 'object' && 'success' in data && data.success === false) {
    throw new Error(data.detail || data.error || data.message || 'Request failed');
  }
  return data;
}

export const api = {
  // System
  health: () => request<{ status: string; version: string }>('/health'),
  shutdown: () => request<{ status: string }>('/shutdown', { method: 'POST' }),

  // Auth
  authStatus: () =>
    request<{ auth_required: boolean; has_password: boolean; nonce_active: boolean }>('/auth/status'),
  authLogin: (password: string) =>
    request<{ token: string; expires_at: string }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ password })
    }),
  authSetPassword: (newPassword: string, currentPassword?: string) =>
    request<{ ok: boolean }>('/auth/set-password', {
      method: 'POST',
      body: JSON.stringify({ new_password: newPassword, current_password: currentPassword ?? null })
    }),
  authLogout: () =>
    request<{ ok: boolean }>('/auth/logout', { method: 'POST' }),

  // Scanner
  scanStart: (type = 'deep', searchQuery = '', pages = 1, source = 'HDEncode', flags?: Record<string, boolean>) =>
    request('/scan/start', {
      method: 'POST',
      body: JSON.stringify({ type, search_query: searchQuery, pages, source, flags: flags ?? null })
    }),
  scanStop: () => request('/scan/stop', { method: 'POST' }),
  scanStatus: () =>
    request<{ state: string; progress: number; phase: string }>('/scan/status'),

  // Results
  getResults: (params?: Record<string, string>) => {
    const qs = params ? '?' + new URLSearchParams(params).toString() : '';
    return request<ResultsResponse>(`/results${qs}`);
  },
  getCachedResults: (params?: Record<string, string>) => {
    const qs = params ? '?' + new URLSearchParams(params).toString() : '';
    return request<CachedResultsResponse>(`/results/cached${qs}`);
  },
  selectItems: (groupKeys: string[], selected: boolean) =>
    request('/results/select', {
      method: 'POST',
      body: JSON.stringify({ group_keys: groupKeys, selected })
    }),
  selectAll: (payload?: Record<string, string>) =>
    request('/results/select-all', {
      method: 'POST',
      body: payload ? JSON.stringify(payload) : undefined
    }),
  deselectAll: () => request('/results/deselect-all', { method: 'POST' }),
  exportCsv: () =>
    request<{ filepath: string }>('/results/export', { method: 'POST' }),
  dismissItems: (urls: string[], titles?: Record<string, string>, dismissed = true) =>
    request<{ status: string; dismissed_count: number }>('/results/dismiss', {
      method: 'POST',
      body: JSON.stringify({ urls, titles: titles ?? null, dismissed })
    }),
  dismissedList: () =>
    request<{ items: { url: string; title: string | null; dismissed_at: string }[]; count: number }>(
      '/results/dismissed'
    ),
  clearDismissed: () =>
    request<{ status: string; dismissed_count: number }>('/results/dismissed', { method: 'DELETE' }),

  // Plex
  plexConnect: () => request('/plex/connect', { method: 'POST' }),
  plexStatus: () => request<PlexStatus>('/plex/status'),
  plexLibraries: () =>
    request<{ movie_libraries: string[]; tv_libraries: string[]; known_libraries: string[] }>(
      '/plex/libraries'
    ),
  updatePlexLibraries: (movieLibraries: string[], tvLibraries: string[]) =>
    request('/plex/libraries', {
      method: 'PUT',
      body: JSON.stringify({ movie_libraries: movieLibraries, tv_libraries: tvLibraries })
    }),
  plexStats: () => request<Record<string, number>>('/plex/stats'),
  plexRefresh: () => request('/plex/refresh', { method: 'POST' }),

  // Downloads
  download: (url: string, title: string, serviceType = 'Rapidgator', year?: number | null,
             resolution = '', size = '', hdr = '', dovi = false) =>
    request('/download', {
      method: 'POST',
      body: JSON.stringify({ url, title, service_type: serviceType, year: year ?? null,
                             resolution, size, hdr, dovi })
    }),
  downloadBatch: (items: { url: string; title: string; year?: number | null; resolution?: string; size?: string; hdr?: string; dovi?: boolean }[], serviceType = 'Rapidgator') =>
    request('/download/batch', {
      method: 'POST',
      body: JSON.stringify({ items: items.map(i => ({
        url: i.url, title: i.title, year: i.year ?? null,
        resolution: i.resolution ?? '', size: i.size ?? '', hdr: i.hdr ?? '', dovi: i.dovi ?? false,
        service_type: serviceType,
      })) })
    }),
  scrapeLinks: (url: string, serviceType = 'Rapidgator', title = '', resolution = '') =>
    request<{ links: string[]; count: number }>('/download/scrape', {
      method: 'POST',
      body: JSON.stringify({ url, service_type: serviceType, title, resolution })
    }),
  copyLinksBatch: (items: { url: string; title?: string; resolution?: string }[], serviceType = 'Rapidgator') =>
    request<{ status: string; count: number }>('/download/copy-links', {
      method: 'POST',
      body: JSON.stringify({ items: items.map(i => ({ url: i.url, service_type: serviceType, title: i.title ?? '', resolution: i.resolution ?? '' })) })
    }),
  openInPlex: (
    title: string,
    imdbId?: string,
    plexRatingKey?: string
  ) =>
    request('/download/open-plex', {
      method: 'POST',
      body: JSON.stringify({
        title,
        imdb_id: imdbId,
        plex_rating_key: plexRatingKey
      })
    }),
  downloadHistory: (limit = 100) =>
    request<DownloadHistoryEntry[]>(`/download/history?limit=${limit}`),
  jdTest: () =>
    request<{ connected: boolean; device?: string; error?: string }>('/download/jd-test'),
  jdStatus: () => request<JdStatus>('/download/jd-status'),
  jdState: () => request<{ connected: boolean; state: JdRunState; error?: string }>('/download/jd-state'),
  jdControl: (action: 'start' | 'stop' | 'pause' | 'resume') =>
    request<{ ok: boolean; action?: string; state?: JdRunState; error?: string }>('/download/jd-control', {
      method: 'POST',
      body: JSON.stringify({ action })
    }),
  downloadResults: (limit = 200) =>
    request<DownloadResult[]>(`/download/results?limit=${limit}`),
  clearDownloadResults: () =>
    request<{ status: string }>('/download/results', { method: 'DELETE' }),

  // Settings
  getSettings: () => request<Settings>('/settings'),
  updateSettings: (updates: Settings) =>
    request('/settings', {
      method: 'PUT',
      body: JSON.stringify(updates)
    }),
  testNotification: (channel: string) =>
    request<{ success: boolean; message: string }>(`/settings/test/${channel}`, {
      method: 'POST'
    }),

  // Sources
  getSources: () =>
    request<{ id: string; name: string; enabled: boolean }[]>('/sources'),
  toggleSource: (id: string, enabled: boolean) =>
    request(`/sources/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ enabled })
    }),

  // Analytics
  analyticsSummary: () => request<AnalyticsSummary>('/analytics/summary'),
  analyticsRenames: () => request<RenameStats>('/analytics/renames'),
  analyticsLibrary: (mode = 'Movies') =>
    request<LibraryStats>(`/analytics/library?mode=${encodeURIComponent(mode)}`),
  analyticsTrends: (days = 30) =>
    request<TrendData>(`/analytics/trends?days=${days}`),

  // Watchlist
  watchlistList: (status?: string, itemType?: string) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (itemType) params.set('item_type', itemType);
    const qs = params.toString();
    return request<WatchlistItem[]>(`/watchlist${qs ? '?' + qs : ''}`);
  },
  watchlistStats: () => request<WatchlistStats>('/watchlist/stats'),
  watchlistSearch: (q: string) =>
    request<WatchlistItem[]>(`/watchlist/search?q=${encodeURIComponent(q)}`),
  watchlistGet: (id: number) => request<WatchlistItem>(`/watchlist/${id}`),
  watchlistAdd: (item: Partial<WatchlistItem>) =>
    request<{ id: number; status: string }>('/watchlist', {
      method: 'POST',
      body: JSON.stringify(item)
    }),
  watchlistUpdate: (id: number, updates: Partial<WatchlistItem>) =>
    request<{ status: string }>(`/watchlist/${id}`, {
      method: 'PUT',
      body: JSON.stringify(updates)
    }),
  watchlistRemove: (id: number) =>
    request<{ status: string }>(`/watchlist/${id}`, { method: 'DELETE' }),
  watchlistExport: () => request<WatchlistExport>('/watchlist/export/json'),
  watchlistImportJson: (data: string) =>
    request<{ imported: number }>('/watchlist/import/json', {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: data
    }),
  watchlistImportImdb: (data: string) =>
    request<{ imported: number }>('/watchlist/import/imdb', {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: data
    }),
  watchlistImportLetterboxd: (data: string) =>
    request<{ imported: number }>('/watchlist/import/letterboxd', {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: data
    }),

  // Scan history
  scanHistory: (limit = 20) =>
    request<{ id: number; timestamp: string; scan_type: string; items_scanned: number; missing_count: number; upgrade_count: number; duration_seconds: number; sources_scanned: string }[]>(`/analytics/scan-history?limit=${limit}`),

  // Trakt import
  watchlistImportTrakt: (username: string, listType = 'watchlist') =>
    request<{ imported: number; total_in_list: number }>('/watchlist/import/trakt', {
      method: 'POST',
      body: JSON.stringify({ username, list_type: listType })
    }),

  // Discovery
  discover: (category = 'trending', page = 1) =>
    request<{ items: { id: number; title: string; year: string | null; overview: string; poster_url: string; rating: number; votes: number }[]; total_pages: number }>(`/discover?category=${category}&page=${page}`),

  // Scheduler
  schedulerStatus: () =>
    request<{ enabled: boolean; interval_hours: number; idle_only: boolean; last_run: string | null; next_run: string | null; scheduler_active: boolean }>('/scheduler/status'),
  schedulerUpdate: (config: { enabled?: boolean; interval_hours?: number; idle_only?: boolean }) =>
    request('/scheduler/config', {
      method: 'PUT',
      body: JSON.stringify(config)
    }),
  schedulerTrigger: () =>
    request<{ status: string }>('/scheduler/trigger', { method: 'POST' }),

  // Background pre-cache scanner
  getBackgroundStatus: () => request<BackgroundStatus>('/background/status'),
  triggerBackgroundScan: () =>
    request<{ status: string }>('/background/scan-now', { method: 'POST' }),

  // Auto-rename
  getRenameJobs: (status?: string) => {
    const qs = status ? `?status=${encodeURIComponent(status)}` : '';
    return request<{ jobs: RenameJob[]; counts: Record<string, number> }>(`/rename/jobs${qs}`);
  },
  getRenameStatus: () => request<RenameStatus>('/rename/status'),
  applyRename: (id: number) =>
    request<{ ok: boolean }>(`/rename/jobs/${id}/apply`, { method: 'POST' }),
  undoRename: (id: number) =>
    request<{ ok: boolean }>(`/rename/jobs/${id}/undo`, { method: 'POST' }),
  rematchRename: (id: number, tmdbId: number, mediaType?: string, season?: number, episode?: number) =>
    request<RematchConfirmResponse>(`/rename/jobs/${id}/rematch`, {
      method: 'POST',
      body: JSON.stringify({
        tmdb_id: tmdbId,
        media_type: mediaType ?? null,
        season: season ?? null,
        episode: episode ?? null
      })
    }),
  acceptCombinedRename: (id: number) =>
    request<{ ok: boolean }>(`/rename/jobs/${id}/accept-combined`, { method: 'POST' }),
  acceptCorrectionRename: (id: number) =>
    request<{ ok: boolean; new_filename?: string }>(`/rename/jobs/${id}/accept-correction`, { method: 'POST' }),
  deleteRenameJob: (id: number) =>
    request<{ ok: boolean }>(`/rename/jobs/${id}`, { method: 'DELETE' }),
  bulkApply: (ids: number[]) =>
    request<BulkApplyResponse>('/rename/jobs/bulk/apply', {
      method: 'POST',
      body: JSON.stringify({ ids })
    }),
  bulkReidentify: (ids: number[]) =>
    request<BulkReidentifyResponse>('/rename/jobs/bulk/reidentify', {
      method: 'POST',
      body: JSON.stringify({ ids })
    }),
  bulkDelete: (ids: number[]) =>
    request<BulkDeleteResponse>('/rename/jobs/bulk/delete', {
      method: 'POST',
      body: JSON.stringify({ ids })
    }),
  bulkSetDestination: (ids: number[], destinationRoot: string) =>
    request<BulkSetDestResponse>('/rename/jobs/bulk/set-destination', {
      method: 'POST',
      body: JSON.stringify({ ids, destination_root: destinationRoot })
    }),
  applyConfident: (ids?: number[]) =>
    request<ApplyConfidentResponse>('/rename/jobs/apply-confident', {
      method: 'POST',
      body: JSON.stringify(ids ? { ids } : {})
    }),
  searchTmdb: (query: string, mediaType: string) => {
    const qs = '?' + new URLSearchParams({ query, media_type: mediaType }).toString();
    return request<{ results: TmdbSearchResult[] }>(`/rename/search-tmdb${qs}`);
  },
  rematchPreview: (
    id: number,
    body: { tmdb_id: number; media_type: string; season?: number; episode?: number }
  ) =>
    request<RematchPreviewResponse>(`/rename/jobs/${id}/rematch-preview`, {
      method: 'POST',
      body: JSON.stringify({
        tmdb_id: body.tmdb_id,
        media_type: body.media_type,
        season: body.season ?? null,
        episode: body.episode ?? null
      })
    }),
  testOllama: () =>
    request<{ ok: boolean; models?: string[]; error?: string }>('/rename/llm/test'),
  renameProcessFolder: (folder: string, dryRun = false) =>
    request<{ status: string; folder: string; dry_run: boolean }>('/rename/process-folder', {
      method: 'POST',
      body: JSON.stringify({ folder, dry_run: dryRun })
    }),
  renameHealth: () =>
    request<{
      binaries: Record<string, boolean>;
      capabilities: Record<string, boolean>;
      ollama: { ok: boolean; model: string; model_available: boolean; error?: string };
      llm_enabled: boolean;
    }>('/rename/health'),
  reidentifyRename: (id: number) =>
    request<{ ok: boolean; job_id?: number; error?: string }>(`/rename/jobs/${id}/reidentify`, { method: 'POST' }),
  reidentifyAllRenames: () =>
    request<{ status: string }>('/rename/reidentify-all', { method: 'POST' }),
  dvScanFolder: (folder: string, force = false) =>
    request<{ status: string; folder: string; force: boolean }>('/rename/dv-scan-folder', {
      method: 'POST',
      body: JSON.stringify({ folder, force })
    }),
  getDvScans: (layer?: string) => {
    const qs = layer ? `?layer=${encodeURIComponent(layer)}` : '';
    return request<{ scans: DvScan[]; counts: Record<string, number> }>(`/rename/dv-scans${qs}`);
  },
};
