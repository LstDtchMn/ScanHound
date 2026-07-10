export interface ScanResult {
  title: string;
  year: number | null;
  season: number | null;
  episodes: number | null;
  resolution: string;
  size: string;
  status: string;
  status_text: string;
  color: string;
  url: string;
  group_key: string;
  rating: number | null;
  votes: number | null;
  votes_source: string;
  rt_score: number | null;
  genres: string[];
  language: string;
  poster_url: string;
  imdb_id: string | null;
  description: string;
  hdr: string;
  dovi: boolean;
  selected: boolean;
  plex_info: string;
  plex_versions: string;
  plex_rating_key: string | null;
  posted_date: string | null;
  host_pref: string;
  is_duplicate_group: boolean;
  prior_grab?: { resolution: string; size: string; downloaded_at: string; hdr?: string; dovi?: boolean } | null;
  category?: string; // crawl category: '4k' | 'remux' | 'tv' | '' (unknown)
}

export interface ScanStats {
  total: number;
  missing: number;
  upgrade: number;
  library: number;
}

export interface ResultsResponse {
  items: ScanResult[];
  total: number;
  page: number;
  per_page: number;
  stats: ScanStats;
  filtered_stats?: ScanStats;
}

export interface CachedResultsResponse extends ResultsResponse {
  source: string;
  last_updated: string | null;
  /** Server-side facets (D3) — genres/languages across the whole matching
   *  set, not just the current page. Present when the backend was asked to
   *  include_facets (currently always, for /results/cached). */
  available_genres?: string[];
  available_languages?: string[];
}

export interface BackgroundStatus {
  enabled: boolean;
  interval_hours: number;
  pages: number;
  sources: string[];
  retain_days: number;
  last_run_at: string | null;
  next_run_at: string | null;
  cached_count: number;
  running: boolean;
}

export interface RenameJob {
  id: number;
  package_name: string | null;
  original_path: string;
  original_filename: string | null;
  new_filename: string | null;
  destination_path: string | null;
  status: string; // pending | matched | needs_review | applied | failed | reverted
  media_type: string | null;
  title: string | null;
  year: number | null;
  season: number | null;
  episode: number | null;
  tmdb_id: number | null;
  imdb_id: string | null;
  resolution: string | null;
  match_confidence: number | null;
  match_source: string | null; // deterministic | llm | manual
  match_reasons?: string[] | null; // why a match is < 100% certain
  move_method: string | null;
  warning_message: string | null;
  error_message: string | null;
  plex_sort_title: string | null;
  detected_at: string | null;
  processed_at: string | null;
  reverted_at: string | null;
  suggested_correction?: {
    original: { season: number; episode: number; title?: string };
    proposed: { season: number; episode: number; title?: string };
  } | null;
  combined_episode?: {
    episode_start: number;
    episode_end: number;
    proposed_code: string;
    runtime_match_pct: number;
  } | null;
  split_file?: {
    part: number;
    sibling_path: string;
  } | null;
  // True when another active job targets the same destination file (e.g. two
  // releases of the same movie). Computed server-side on the jobs list.
  destination_conflict?: boolean;
  // Within a duplicate group, the best-quality release to keep (with a short
  // reason like "2160p · Dolby Vision · Remux"). Computed server-side.
  keep_recommended?: boolean;
  keep_reason?: string | null;
  /** Fully-formed TMDB poster URL built server-side from poster_path. Empty/null = no poster. */
  poster_url?: string | null;
  /** Read-only DV layer joined from dv_scan by path at serialize time (FEL/MEL/P8/P5). Null = unknown. */
  dv_layer?: string | null;
}

export interface DvScan {
  path: string;
  title: string | null;
  dv_layer: string; // fel | mel | profile5 | profile8 | none | unknown
  rating_key?: string | null;
  imdb_id?: string | null;
  scanned_at?: string | null;
  last_seen_at?: string | null;
}

export interface RenameStats {
  applied: number;
  total_jobs: number;
  by_status: Record<string, number>;
  by_directory: Record<string, number>;
  by_method: Record<string, number>;
}

export interface RenameStatus {
  enabled: boolean;
  require_confirmation: boolean;
  confidence_threshold: number;
  move_method: string;
  llm_enabled: boolean;
  counts: Record<string, number>;
  needs_review: number;
}

export interface WsMessage {
  type: string;
  data: Record<string, unknown>;
}

export interface PlexStatus {
  connected: boolean;
  server: string;
  movie_count: number;
  tv_count: number;
}

export interface HealthResponse {
  status: string;
  version: string;
  plex_connected: boolean;
}

export interface LibraryStats {
  total_items: number;
  total_size_gb: number;
  resolution_counts: Record<string, number>;
  resolution_sizes: Record<string, number>;
  hdr_count: number;
  dovi_count: number;
  sdr_count: number;
  codec_counts: Record<string, number>;
  quality_score: number;
  upgrade_potential: number;
}

export interface AnalyticsSummary {
  generated_at: string;
  library: {
    movies: LibraryStats;
    tv_shows: LibraryStats;
    total_items: number;
    total_size_gb: number;
    overall_quality_score: number;
  };
  scans: {
    total_scans: number;
    avg_duration: number;
    total_items_scanned: number;
    total_missing_found: number;
    total_upgrades_found: number;
    last_scan_time: string | null;
    scans_per_day: Record<string, number>;
    avg_items_per_scan: number;
  };
  trends: {
    dates: string[];
    items_scanned: number[];
    missing_found: number[];
    upgrades_found: number[];
    avg_duration: number[];
    scan_count: number[];
  };
  quality_breakdown: {
    movies: { resolution: { labels: string[]; counts: number[]; sizes: number[] }; hdr: { labels: string[]; counts: number[] } };
    tv_shows: { resolution: { labels: string[]; counts: number[]; sizes: number[] }; hdr: { labels: string[]; counts: number[] } };
  };
}

export interface WatchlistItem {
  id: number;
  title: string;
  year: number | null;
  imdb_id: string | null;
  tmdb_id: string | null;
  item_type: string;
  status: string;
  season: number | null;
  min_resolution: string | null;
  prefer_dovi: boolean;
  notes: string;
  added_date: string | null;
  found_date: string | null;
  found_url: string | null;
  priority: number;
  poster_url?: string;
}

export interface WatchlistStats {
  total: number;
  by_status: Record<string, number>;
  by_type: Record<string, number>;
  recent_additions: number;
  recently_found: number;
}

export interface WatchlistExport {
  exported_at: string;
  count: number;
  items: WatchlistItem[];
}

export interface TrendData {
  dates: string[];
  items_scanned: number[];
  missing_found: number[];
  upgrades_found: number[];
  avg_duration: number[];
  scan_count: number[];
}

/** All application settings. Every field is optional to support partial updates. */
export interface Settings {
  // Plex Connection
  plex_url?: string;
  plex_token?: string;
  plex_server_id?: string;
  plex_connection_mode?: string;
  plex_username?: string;
  plex_password?: string;
  plex_server_name?: string;

  // API Keys
  tmdb_api_key?: string;
  omdb_api_key?: string;
  use_tmdb?: boolean;

  // Size & Resolution
  min_size_mb?: number;
  pref_res?: string;

  // Display Options
  show_rating?: boolean;
  show_votes?: boolean;
  show_rt?: boolean;
  show_rg?: boolean;
  show_nf?: boolean;
  show_links?: boolean;
  show_genres?: boolean;

  // Cache Settings
  cache_duration?: number;
  plex_refresh_mode?: string;
  plex_invalidate_on_new_content?: boolean;

  // Filtering
  ignore_keywords?: string;

  // Upgrade Rules
  upgrade_sensitivity?: number;
  upgrade_dv_loss_sensitivity?: number;
  rule_1080_4k?: boolean;
  rule_1080_4k_size?: boolean;
  rule_1080_1080?: boolean;
  rule_4k_4k?: boolean;
  rule_dv?: boolean;
  strict_resolution?: boolean;

  // Libraries
  movie_libs?: string[];
  tv_libs?: string[];
  known_libraries?: string[];

  // Download
  download_dir?: string;
  download_service_type?: string;

  // JDownloader Integration
  jd_enabled?: boolean;
  jd_method?: string;
  jd_folder?: string;
  jd_movies_folder?: string;
  jd_movies_folder_4k?: string;
  jd_tv_folder?: string;
  jd_email?: string;
  jd_password?: string;
  jd_device?: string;

  // Filtering
  exclude_720p?: boolean;

  // Sources
  source_2160p?: boolean;
  source_remux?: boolean;
  source_tv_packs?: boolean;

  // DDLBase / Cuty.io
  ddlbase_enabled?: boolean;
  ddlbase_manual_resolution_timeout?: number;
  cuty_email?: string;
  cuty_password?: string;

  // Adit-HD Forum
  adithd_enabled?: boolean;
  adithd_username?: string;
  adithd_password?: string;
  adithd_auto_reply?: boolean;
  adithd_preferred_host?: string;

  // Scheduler
  scheduler_enabled?: boolean;
  scheduler_interval?: number;
  last_scan_time?: number;

  // Background pre-cache scanning
  background_scan_enabled?: boolean;
  background_scan_interval_hours?: number;
  background_scan_pages?: number;
  background_scan_sources?: string[];
  background_scan_retain_days?: number;
  background_scan_last_run?: number;

  // Auto-rename + Plex sort + Ollama assist
  auto_rename_enabled?: boolean;
  auto_rename_confidence_threshold?: number;
  auto_rename_require_confirmation?: boolean;
  auto_rename_move_method?: string;
  auto_rename_movie_library?: string;
  auto_rename_movie_library_4k?: string;
  auto_rename_tv_library?: string;
  auto_rename_template_movie?: string;
  auto_rename_template_tv?: string;
  auto_rename_path_mappings?: string;
  auto_rename_plex_sort_titles?: boolean;
  deletions_require_confirmation?: boolean;
  trash_retention_days?: number;
  auto_rename_llm_enabled?: boolean;
  ollama_base_url?: string;
  ollama_model?: string;
  ollama_vision_model?: string;

  // Dolby Vision host detector + Plex labeling
  dv_library_roots?: string;
  dv_detection?: boolean;
  dv_file_tagging?: boolean;
  dv_label_vocab?: string;

  // Debug & Logging
  debug_mode?: boolean;
  clear_logs_startup?: boolean;
  scan_threads?: number;
  verbose_logging?: boolean;

  // Matching thresholds
  tv_match_threshold?: number;
  low_match_threshold?: number;
  movie_match_threshold?: number;
  year_tolerance?: number;

  // Scanner
  base_url?: string;
  scheduler_only_when_idle?: boolean;

  // Display
  tile_columns?: number;

  // Appearance
  theme_mode?: string;

  // System Tray & Startup
  enable_system_tray?: boolean;
  minimize_to_tray?: boolean;
  start_minimized?: boolean;
  auto_connect_plex?: boolean;

  // Plex Account (remote)
  plex_selected_server?: string;

  // Auto-Grab
  auto_grab_enabled?: boolean;
  auto_grab_min_rating?: number;
  auto_grab_min_votes?: number;
  auto_grab_genres?: string;
  auto_grab_exclude_genres?: string;
  auto_grab_languages?: string;
  auto_grab_statuses?: string;

  // Notifications
  desktop_notifications?: boolean;
  discord_webhook?: string;
  discord_username?: string;
  slack_webhook?: string;
  pushover_user?: string;
  pushover_token?: string;
  webhook_url?: string;
  webhook_method?: string;
  email_enabled?: boolean;
  smtp_host?: string;
  smtp_port?: number;
  smtp_username?: string;
  smtp_password?: string;
  email_from?: string;
  email_to?: string;
  smtp_tls?: boolean;
}

/** One link (file) inside a JDownloader package. */
export interface JdLink {
  name: string;
  host: string;
  availability: string; // ONLINE | OFFLINE | UNKNOWN | TEMP_UNKNOWN
  bytes?: number;
  bytesLoaded?: number;
  stage: string; // linkgrabber | downloading | finished
  status?: string;
}

/** A JDownloader package: the real title plus its child links, grouped so the
 *  UI can render a collapsible folder (mirroring JDownloader's package view). */
export interface JdPackage {
  uuid: string;
  name: string; // raw JD package name (often the obfuscated archive name)
  title: string; // resolved movie/show title (falls back to `name`)
  host: string;
  total: number;
  online: number;
  offline: number;
  bytes_total: number;
  bytes_loaded: number;
  stage: string; // linkgrabber | downloading | finished | mixed
  links: JdLink[];
}

/** JDownloader global download-queue run state. */
export type JdRunState = 'running' | 'paused' | 'stopped' | 'unknown';

export interface JdStatus {
  connected: boolean;
  error?: string;
  total: number; // total link count across all packages
  online: number;
  offline: number;
  package_count?: number;
  truncated?: boolean; // true when more packages exist than were returned
  packages: JdPackage[];
  state?: JdRunState;
}

/** One row from the download history table (completed/clipboard/browser/failed). */
export interface DownloadHistoryEntry {
  url: string;
  title: string;
  resolution: string;
  size: string;
  downloaded_at: string;
  status: string;
  path?: string; // optional/legacy — not returned by the current API
  timestamp?: string; // optional/legacy fallback for downloaded_at
}

/** Persisted per-item download + extraction outcome, polled from JDownloader. */
export interface DownloadResult {
  name: string; // JDownloader package name
  title: string; // resolved movie/show title
  host: string;
  bytes_total: number;
  bytes_loaded: number;
  downloaded: number; // 0 | 1
  extraction: 'na' | 'running' | 'success' | 'error' | string;
  state: 'queued' | 'downloading' | 'downloaded' | 'extracting' | 'extracted' | 'failed' | string;
  error: string | null;
  updated_at: string;
}

export interface TmdbSearchResult {
  tmdb_id: number;
  title: string;
  year: number | null;
  media_type: 'movie' | 'tv';
  poster_url: string | null;
}

export interface BulkApplyResult {
  id: number;
  ok: boolean;
  error: string | null;
}
export interface BulkApplyResponse {
  // Applies are queued to a background worker (cross-device moves can take
  // minutes); per-job outcomes arrive over the rename:job WS event.
  ok?: boolean;
  queued?: number;
  skipped?: number;
  // Legacy synchronous fields (older servers):
  results?: BulkApplyResult[];
  applied?: number;
  failed?: number;
}

export interface BulkReidentifyResponse {
  ok: boolean;
  queued: number;
}

export interface BulkDeleteResponse {
  deleted: number;
}

export interface BulkSetDestResult {
  id: number;
  ok: boolean;
  destination_path: string | null;
  error: string | null;
}
export interface BulkSetDestResponse {
  results: BulkSetDestResult[];
  updated: number;
}

export interface ApplyConfidentResponse {
  ok?: boolean;
  queued?: number;
  skipped?: number;
  // Legacy synchronous fields (older servers):
  results?: BulkApplyResult[];
  applied?: number;
  failed?: number;
}

export interface FileSpec {
  present: boolean; path: string | null; size_bytes: number | null;
  resolution: string | null; video_codec: string | null; hdr: string | null;
  dv_layer: string | null; audio: string | null;
  duration_min: number | null; bitrate: number | null;
}
export interface ConflictComparison {
  existing: FileSpec | null;
  // null when the job wasn't found (backend conflict_preview returns
  // {existing:null, incoming:null, ...} in that case) — consumers must null-check.
  incoming: FileSpec | null;
  recommended: 'existing' | 'incoming' | 'tie' | null;
  reason: string | null;
}

export interface RematchPreviewResponse {
  new_filename: string;
  destination_path: string | null;
  library_configured: boolean;
  warning: string | null;
}

export interface RematchConfirmResponse {
  ok: boolean;
  status: string;
  new_filename: string;
  destination_path: string | null;
  warning: string | null;
}

export interface TrashEntry {
  bucket: string;
  name: string;
  size: number;
  trashed_at: string | null;
  original_path: string | null;
  restorable: boolean;
}

export interface TrashListResponse {
  entries: TrashEntry[];
}

export interface TrashRestoreResponse {
  ok: boolean;
  restored_path?: string;
  error?: string;
}

export interface RenameHealthResponse {
  binaries: Record<string, boolean>;
  capabilities: Record<string, boolean>;
  ollama: { ok: boolean; model: string; model_available: boolean; error?: string };
  llm_enabled: boolean;
  failed_db_last_package: number;
  db_corruption_flag: boolean;
}
