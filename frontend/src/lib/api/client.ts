import type { ResultsResponse, CachedResultsResponse, BackgroundStatus, RenameJob, RenameStatus, RenameStats, DvScan, PlexStatus, PlexMetadataScanStatus, AnalyticsSummary, LibraryStats, TrendData, WatchlistItem, WatchlistStats, WatchlistExport, Settings, JdStatus, JdRunState, DownloadResult, DownloadHistoryEntry, BulkApplyResponse, BulkReidentifyResponse, BulkDeleteResponse, BulkSetDestResponse, ApplyConfidentResponse, TmdbSearchResult, RematchPreviewResponse, RematchConfirmResponse, TrashListResponse, TrashRestoreResponse, TrashDeleteResponse, TrashEmptyResponse, RenameHealthResponse, ConflictComparison, PipelineItem, PipelineCounts, AlternativeRelease, SearchSourcesResponse, ScanResult } from './types';
import { apiBase, getStoredToken } from './endpoint';

const REQUEST_TIMEOUT_MS = 15_000;

export type PublicErrorDetail = {
  code?: string;
  message?: string;
  correlation_id?: string;
};

export function formatErrorDetail(detail: unknown): string | undefined {
  if (typeof detail === 'string') {
    return detail;
  }
  if (!detail || typeof detail !== 'object') {
    return undefined;
  }
  const candidate = detail as PublicErrorDetail;
  if (typeof candidate.message !== 'string' || !candidate.message.trim()) {
    return undefined;
  }
  if (typeof candidate.correlation_id === 'string' && candidate.correlation_id) {
    return `${candidate.message} (Reference: ${candidate.correlation_id})`;
  }
  return candidate.message;
}


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

async function request<T>(path: string, options?: RequestInit, timeoutMs = REQUEST_TIMEOUT_MS): Promise<T> {
  const headers = new Headers(options?.headers);
  if (options?.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (authNonce) {
    headers.set('Authorization', `Bearer ${authNonce}`);
  }

  const resp = await fetchWithTimeout(`${apiBase()}${path}`, { ...options, headers }, timeoutMs);
  if (!resp.ok) {
    if (resp.status === 401 && !path.startsWith('/auth/')) {
      unauthorizedHandler?.();
    }
    // FastAPI's HTTPException(detail=...) responses are JSON — surface that
    // message (e.g. "Destination already exists: ...") when present, so a
    // caller-shown error is actionable instead of a bare status code.
    let detail: string | undefined;
    try {
      const body = await resp.clone().json();
      detail = formatErrorDetail(body?.detail);
    } catch {
      // non-JSON error body — fall through to the generic message
    }
    throw new Error(detail || `API error: ${resp.status} ${resp.statusText}`);
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
    request<{
      auth_required: boolean;
      has_password: boolean;
      nonce_active: boolean;
      setup_required: boolean;
    }>('/auth/status'),
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
  dismissItems: (
    urls: string[],
    titles?: Record<string, string>,
    dismissed = true,
    meta?: Record<string, { group_key?: string; resolution?: string; dovi?: boolean }>
  ) =>
    request<{ status: string; dismissed_count: number }>('/results/dismiss', {
      method: 'POST',
      body: JSON.stringify({ urls, titles: titles ?? null, meta: meta ?? null, dismissed })
    }),
  dismissedList: () =>
    request<{ items: { url: string; title: string | null; dismissed_at: string }[]; count: number }>(
      '/results/dismissed'
    ),
  clearDismissed: () =>
    request<{ status: string; dismissed_count: number }>('/results/dismissed', { method: 'DELETE' }),
  setBookmark: (
    imdbId: string | null,
    title: string,
    year: number | null,
    mediaType: string,
    bookmarked: boolean
  ) =>
    request<{ status: string; bookmarked: boolean }>('/results/bookmark', {
      method: 'POST',
      body: JSON.stringify({ imdb_id: imdbId, title, year, media_type: mediaType, bookmarked })
    }),
  getBookmarks: () =>
    request<{
      items: { id: number; imdb_id: string | null; title: string; year: number | null; media_type: string; created_at: string }[];
      count: number;
    }>('/results/bookmarks'),

  // HDEncode RSS operations
  rssStatus: () => request<any>('/rss/status'),
  rssCandidates: (state?: string, hydration?: string, limit = 250) => {
    const params = new URLSearchParams();
    if (state) params.set('state', state);
    if (hydration) params.set('hydration', hydration);
    params.set('limit', String(limit));
    return request<{ items: any[]; count: number }>(`/rss/candidates?${params}`);
  },
  rssHydration: (limit = 250) =>
    request<{ items: any[]; count: number }>(`/rss/hydration?limit=${limit}`),
  rssSetMode: (mode: 'listing' | 'rss_shadow' | 'rss_primary') =>
    request<{ mode: string }>('/rss/mode', {
      method: 'POST',
      body: JSON.stringify({ mode })
    }),
  rssHydrate: (canonicalUrl: string) =>
    request<{ status: string; canonical_url: string }>('/rss/hydrate', {
      method: 'POST',
      body: JSON.stringify({ canonical_url: canonicalUrl })
    }),
  rssRetry: (canonicalUrl: string) =>
    request<{ status: string; canonical_url: string }>('/rss/retry', {
      method: 'POST',
      body: JSON.stringify({ canonical_url: canonicalUrl })
    }),

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
  // Bulk library metadata scan (probe_specs + DV FEL/MEL) -- movies only.
  plexScanMetadata: (scope: 'all' | 'selected', ids?: string[]) =>
    request<{ status: string; total?: number }>('/plex/scan-metadata', {
      method: 'POST',
      body: JSON.stringify({ scope, ids: ids ?? null })
    }),
  plexScanMetadataCancel: () =>
    request<{ status: string }>('/plex/scan-metadata/cancel', { method: 'POST' }),
  plexScanMetadataStatus: () => request<PlexMetadataScanStatus>('/plex/scan-metadata/status'),
  getUnmappedPlexPaths: () => request<{ prefixes: string[] }>('/plex/unmapped-paths'),

  // Downloads
  download: (url: string, title: string, serviceType = 'Rapidgator', year?: number | null,
             resolution = '', size = '', hdr = '', dovi = false, season?: number | null) =>
    request('/download', {
      method: 'POST',
      body: JSON.stringify({ url, title, service_type: serviceType, year: year ?? null,
                             resolution, size, hdr, dovi, season: season ?? null })
    }),
  downloadBatch: (items: { url: string; title: string; year?: number | null; season?: number | null; resolution?: string; size?: string; hdr?: string; dovi?: boolean }[], serviceType = 'Rapidgator') =>
    request('/download/batch', {
      method: 'POST',
      body: JSON.stringify({ items: items.map(i => ({
        url: i.url, title: i.title, year: i.year ?? null, season: i.season ?? null,
        resolution: i.resolution ?? '', size: i.size ?? '', hdr: i.hdr ?? '', dovi: i.dovi ?? false,
        service_type: serviceType,
      })) })
    }),
  scrapeLinks: (url: string, serviceType = 'Rapidgator', title = '', resolution = '') =>
    request<{ links: string[]; count: number }>('/download/scrape', {
      method: 'POST',
      body: JSON.stringify({ url, service_type: serviceType, title, resolution })
    }),
  rescanItem: (url: string) =>
    request<{ status: string; item: ScanResult }>('/scan/rescan-item', {
      method: 'POST',
      body: JSON.stringify({ url })
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
  removeDownloadResult: (id: number) =>
    request<{ ok: boolean; removed: number }>('/download/results/remove', {
      method: 'POST',
      body: JSON.stringify({ id })
    }),

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
  getRenameJobs: (status?: string, archived?: boolean) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (archived) params.set('archived', 'true');
    const qs = params.toString() ? `?${params.toString()}` : '';
    return request<{ jobs: RenameJob[]; counts: Record<string, number> }>(`/rename/jobs${qs}`);
  },
  getRenameStatus: () => request<RenameStatus>('/rename/status'),
  applyRename: (id: number, body?: {
    conflict_strategy?: 'overwrite' | 'keep_both' | 'skip' | 'replace_library_dup';
  }) =>
    request<{ ok: boolean }>(`/rename/jobs/${id}/apply`,
      { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  keepPlexRename: (id: number) =>
    request<{ ok: boolean; warning?: string | null }>(
      `/rename/jobs/${id}/keep-plex`, { method: 'POST' }),
  conflictPreview: (id: number) =>
    request<ConflictComparison>(`/rename/jobs/${id}/conflict-preview`, { method: 'POST' }),
  scanConflictDv: (id: number) =>
    request<{ status: string }>(`/rename/jobs/${id}/scan-dv-conflict`, { method: 'POST' }),
  undoRename: (id: number) =>
    request<{ ok: boolean; restore_warning?: string | null }>(`/rename/jobs/${id}/undo`, { method: 'POST' }),
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
  bulkArchive: (ids: number[]) =>
    request<{ archived: number }>('/rename/jobs/bulk/archive', {
      method: 'POST',
      body: JSON.stringify({ ids })
    }),
  bulkUnarchive: (ids: number[]) =>
    request<{ unarchived: number }>('/rename/jobs/bulk/unarchive', {
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
  cancelApply: () =>
    request<{ ok: boolean }>('/rename/apply/cancel', { method: 'POST' }),
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
  renameHealth: () => request<RenameHealthResponse>('/rename/health'),
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
  dvImport: () =>
    request<{ imported: number; updated: number }>('/rename/dv-import', {
      method: 'POST',
      body: JSON.stringify({})
    }),
  dvSyncLabels: (dryRun = false) =>
    request<{ status: string }>('/rename/dv-sync-labels', {
      method: 'POST',
      body: JSON.stringify({ dry_run: dryRun })
    }),

  // Trash (recoverable deletes)
  trashList: () => request<TrashListResponse>('/rename/trash'),
  trashRestore: (bucket: string, name: string) =>
    request<TrashRestoreResponse>('/rename/trash/restore', {
      method: 'POST',
      body: JSON.stringify({ bucket, name })
    }),
  trashDelete: (bucket: string, name: string) =>
    request<TrashDeleteResponse>('/rename/trash/delete', {
      method: 'POST',
      body: JSON.stringify({ bucket, name })
    }),
  trashEmpty: () => request<TrashEmptyResponse>('/rename/trash/empty', { method: 'POST' }),

  // Pipeline tracker
  getPipelineItems: (category?: string, includeDismissed = false) => {
    const qs = new URLSearchParams();
    if (category) qs.set('category', category);
    if (includeDismissed) qs.set('include_dismissed', 'true');
    const suffix = qs.toString() ? `?${qs}` : '';
    return request<PipelineItem[]>(`/pipeline/items${suffix}`);
  },
  getPipelineCounts: () => request<PipelineCounts>('/pipeline/counts'),
  dismissPipelineItem: (url: string) =>
    request<{ ok: boolean }>('/pipeline/dismiss', { method: 'POST', body: JSON.stringify({ url }) }),
  regrabPipelineItem: (url: string) =>
    request<{ status: string }>('/pipeline/regrab', { method: 'POST', body: JSON.stringify({ url }) }),
  // Backend allows up to 45s for the multi-source search (asyncio.wait_for(...,
  // timeout=45.0) in backend/api/routes/pipeline.py) — override the client's
  // default 15s timeout so a slow-but-successful search isn't reported as a
  // client-side timeout.
  searchPipelineSources: (url: string) =>
    request<SearchSourcesResponse>(
      '/pipeline/search-sources',
      { method: 'POST', body: JSON.stringify({ url }) },
      50_000
    ),
  // The backend's AlternativeReleaseRequest model only reads these 8 fields
  // (backend/api/routes/pipeline.py) — send just that subset rather than the
  // full AlternativeRelease, which also carries source-only metadata
  // (imdb_id, tmdb_id, codec, audio, search_key, ...) the endpoint ignores.
  // originalUrl is a separate param (not a field on AlternativeRelease,
  // which models a search result and has no notion of "the grab this
  // alternative is replacing") — when present, the backend dismisses the
  // original grab's pipeline verdict once the alternative starts grabbing.
  grabAlternative: (release: AlternativeRelease, originalUrl?: string) =>
    request<{ status: string }>('/pipeline/grab-alternative', {
      method: 'POST',
      body: JSON.stringify({
        display_title: release.display_title,
        url: release.url,
        year: release.year,
        res: release.res,
        size: release.size,
        dovi: release.dovi,
        hdr: release.hdr,
        season: release.season,
        original_url: originalUrl
      })
    }),
};
