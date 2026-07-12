<script lang="ts">
  import { settings, saveSettings, resetSettings, isDirty, loadSettings } from '$lib/stores/settings';
  import { plexConnected, plexServer, refreshPlexStatus, connectPlex } from '$lib/stores/plex';
  import { addToast } from '$lib/stores/notifications';
  import { scanState } from '$lib/stores/scanner';
  import { jdConnection, refreshJdConnection } from '$lib/stores/jdownloader';
  import { api } from '$lib/api/client';
  import ServerConnection from '$lib/components/ServerConnection.svelte';
  import ChangePassword from '$lib/components/ChangePassword.svelte';
  import Tooltip from '$lib/components/Tooltip.svelte';
  import PlexMetadataScanPanel from '$lib/components/settings/PlexMetadataScanPanel.svelte';
  import { serverUrl } from '$lib/stores/server';
  import { onMount } from 'svelte';
  import type { BackgroundStatus } from '$lib/api/types';

  async function testJd() {
    if ($isDirty) await saveSettings();
    await refreshJdConnection();
  }

  let plexConnecting = $state(false);
  let plexRefreshing = $state(false);
  let testingChannel = $state<string | null>(null);
  let triggerLoading = $state(false);
  let saving = $state(false);
  let testResults = $state<Record<string, 'success' | 'error' | null>>({});
  let schedulerStatus = $state<{next_run: string | null; scheduler_active: boolean} | null>(null);
  let backgroundStatus = $state<BackgroundStatus | null>(null);
  let bgScanning = $state(false);
  let ollamaTest = $state<{ ok: boolean; models?: string[]; error?: string } | null>(null);
  let ollamaTesting = $state(false);

  async function testOllamaConnection() {
    ollamaTesting = true;
    ollamaTest = null;
    if ($isDirty) await saveSettings();  // persist the URL before probing
    try {
      ollamaTest = await api.testOllama();
    } catch {
      ollamaTest = { ok: false, error: 'Request failed' };
    } finally {
      ollamaTesting = false;
    }
  }
  let knownLibraries = $state<string[]>([]);
  let movieLibs = $state<string[]>([]);
  let tvLibs = $state<string[]>([]);
  let libraryError = $state('');

  let mounted = true;

  onMount(() => {
    loadSettings();
    refreshPlexStatus();
    loadLibraries();
    loadSchedulerStatus();
    loadBackgroundStatus();
    return () => { mounted = false; };
  });

  async function loadSchedulerStatus() {
    try {
      const status = await api.schedulerStatus();
      schedulerStatus = { next_run: status.next_run, scheduler_active: status.scheduler_active };
    } catch { /* ignore */ }
  }

  async function loadBackgroundStatus() {
    try {
      backgroundStatus = await api.getBackgroundStatus();
    } catch { /* ignore */ }
  }

  async function runBackgroundScan() {
    bgScanning = true;
    try {
      await api.triggerBackgroundScan();
      addToast('Background scan', 'Started — this runs in the background');
      setTimeout(loadBackgroundStatus, 1500);
    } catch {
      addToast('Background scan', 'Could not start a scan', 'error');
    } finally {
      bgScanning = false;
    }
  }

  function toggleBgSource(source: string, on: boolean) {
    settings.update((s) => {
      const cur = new Set(s.background_scan_sources ?? []);
      if (on) cur.add(source); else cur.delete(source);
      return { ...s, background_scan_sources: [...cur] };
    });
  }

  async function loadLibraries() {
    libraryError = '';
    try {
      const libs = await api.plexLibraries();
      knownLibraries = libs.known_libraries || [];
      movieLibs = libs.movie_libraries || [];
      tvLibs = libs.tv_libraries || [];
    } catch (e) {
      libraryError = e instanceof Error ? e.message : 'Failed to load Plex libraries';
    }
  }

  function toggleLibrary(name: string, type: 'movie' | 'tv') {
    if (type === 'movie') {
      movieLibs = movieLibs.includes(name)
        ? movieLibs.filter(l => l !== name)
        : [...movieLibs, name];
    } else {
      tvLibs = tvLibs.includes(name)
        ? tvLibs.filter(l => l !== name)
        : [...tvLibs, name];
    }
    api.updatePlexLibraries(movieLibs, tvLibs).catch((e) =>
      addToast('Error', e instanceof Error ? e.message : 'Failed to save library assignments', 'error')
    );
  }

  async function handlePlexConnect() {
    plexConnecting = true;
    try {
      await connectPlex();
      let connected = false;
      for (let i = 0; i < 15 && mounted; i += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        if (!mounted) break;
        try {
          const status = await api.plexStatus();
          if (status.connected) {
            connected = true;
            await refreshPlexStatus();
            await loadLibraries();
            break;
          }
        } catch {
          // Status poll failed — keep trying
        }
      }

      if (connected) {
        addToast('Plex', 'Connected successfully');
      } else {
        addToast('Plex', 'Connection timed out', 'error');
      }
    } catch (e) {
      addToast('Plex', e instanceof Error ? e.message : 'Connection failed', 'error');
    } finally {
      plexConnecting = false;
    }
  }

  async function testChannel(channel: string) {
    testingChannel = channel;
    try {
      // Save current settings first so the backend tests the value the user just entered
      await saveSettings();
      const result = await api.testNotification(channel);
      if (result.success) {
        testResults = { ...testResults, [channel]: 'success' };
        addToast('Test', result.message);
      } else {
        testResults = { ...testResults, [channel]: 'error' };
        addToast('Test Failed', result.message, 'error');
      }
    } catch (e) {
      testResults = { ...testResults, [channel]: 'error' };
      addToast('Test Failed', e instanceof Error ? e.message : `Could not test ${channel}`, 'error');
    } finally {
      testingChannel = null;
    }
  }

  async function triggerScan() {
    triggerLoading = true;
    try {
      await api.schedulerTrigger();
      addToast('Scheduler', 'Scan triggered successfully');
      loadSchedulerStatus();
    } catch (e) {
      addToast('Scheduler', e instanceof Error ? e.message : 'Failed to trigger scan', 'error');
    } finally {
      triggerLoading = false;
    }
  }

  type Tab = 'general' | 'connection' | 'plex' | 'sources' | 'notifications' | 'scheduler' | 'background' | 'rename' | 'matching' | 'autograb';
  let activeTab = $state<Tab>('general');

  const tabs: { value: Tab; label: string }[] = [
    { value: 'general', label: 'General' },
    { value: 'connection', label: 'Connection' },
    { value: 'plex', label: 'Plex' },
    { value: 'sources', label: 'Sources' },
    { value: 'matching', label: 'Matching' },
    { value: 'autograb', label: 'Auto-Grab' },
    { value: 'scheduler', label: 'Scheduler' },
    { value: 'background', label: 'Background' },
    { value: 'rename', label: 'Renaming' },
    { value: 'notifications', label: 'Notifications' }
  ];

  const inputClass = 'mt-1 w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]';
  const inputSmClass = 'mt-1 w-32 bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]';
  const testBtnBase = 'px-3 py-1.5 text-xs rounded-lg bg-[var(--bg-tertiary)] hover:bg-[var(--border)] text-[var(--text-primary)] transition-colors disabled:opacity-50';
  const testBtnClass = `${testBtnBase} border border-[var(--border)]`;

  function testBtnClassFor(channel: string): string {
    const r = testResults[channel];
    if (r === 'success') return `${testBtnBase} border-2 border-[var(--success)]`;
    if (r === 'error') return `${testBtnBase} border-2 border-[var(--error)]`;
    return testBtnClass;
  }

  function testBtnLabel(channel: string, name?: string): string {
    const label = name ? `Test ${name}` : 'Test';
    if (testingChannel === channel) return 'Testing...';
    const r = testResults[channel];
    if (r === 'success') return name ? `✓ ${name}` : '✓ Valid';
    if (r === 'error') return name ? `✗ ${name}` : '✗ Failed';
    return label;
  }

  function clearTestResult(channel: string) {
    if (testResults[channel]) {
      testResults = { ...testResults, [channel]: null };
    }
  }

  let scrollEl: HTMLDivElement | undefined;

  function switchTab(tab: Tab) {
    activeTab = tab;
    scrollEl?.scrollTo({ top: 0 });
  }
</script>

<div bind:this={scrollEl} class="flex-1 overflow-auto">
  <div class="sticky top-0 z-10 bg-[var(--bg-primary)] border-b border-[var(--border)] px-4">
    <div class="flex gap-1 overflow-x-auto">
      {#each tabs as tab}
        <button
          class="px-4 py-3 text-sm font-medium border-b-2 transition-colors whitespace-nowrap
            {activeTab === tab.value
              ? 'border-[var(--accent)] text-[var(--accent)]'
              : 'border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)]'}"
          onclick={() => switchTab(tab.value)}
        >
          {tab.label}
        </button>
      {/each}
    </div>
  </div>

  <div class="p-4 md:p-6 max-w-2xl mx-auto w-full">
    {#if activeTab === 'general'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">General Settings</h2>

        <!-- Download Preferences card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Download Preferences</h3>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Minimum File Size (MB)</span>
            <input
              type="number"
              min="0"
              value={$settings.min_size_mb as number ?? 200}
              oninput={(e) => settings.update((s) => ({ ...s, min_size_mb: parseInt(e.currentTarget.value) || 200 }))}
              class={inputSmClass}
            />
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Preferred Resolution</span>
            <select
              value={$settings.pref_res as string ?? 'Prefer 4K'}
              onchange={(e) => settings.update((s) => ({ ...s, pref_res: e.currentTarget.value }))}
              class={inputClass}
            >
              <option value="Prefer 4K">Prefer 4K</option>
              <option value="Prefer 1080p">Prefer 1080p</option>
              <option value="4K Only">4K Only</option>
              <option value="1080p Only">1080p Only</option>
            </select>
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Ignore Keywords (comma-separated)</span>
            <input
              type="text"
              value={$settings.ignore_keywords as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, ignore_keywords: e.currentTarget.value }))}
              placeholder="Cam, TS, HC, KORSUB, TC"
              class={inputClass}
            />
          </label>
        </div>

        <!-- Grid Layout card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Grid Layout</h3>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Tile Columns (0 = auto)</span>
            <input
              type="number"
              min="0"
              max="12"
              value={$settings.tile_columns as number ?? 0}
              oninput={(e) => settings.update((s) => ({ ...s, tile_columns: parseInt(e.currentTarget.value) || 0 }))}
              class={inputSmClass}
            />
          </label>
        </div>

        <!-- Display Columns card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Display Columns</h3>
          <p class="text-xs text-[var(--text-secondary)]">Toggle which columns are visible in results.</p>

          <div class="space-y-2">
            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.show_rating as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, show_rating: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Rating</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.show_votes as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, show_votes: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Vote Count</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.show_rt as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, show_rt: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Rotten Tomatoes Scores</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.show_genres as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, show_genres: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Genres</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.show_links as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, show_links: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">IMDb / Plex Links</span>
            </label>
          </div>
        </div>

        <!-- Advanced card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Advanced</h3>

          <div class="space-y-2">
            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.debug_mode as boolean ?? false}
                onchange={(e) => settings.update((s) => ({ ...s, debug_mode: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Debug Mode</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.verbose_logging as boolean ?? false}
                onchange={(e) => settings.update((s) => ({ ...s, verbose_logging: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Verbose Logging</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.clear_logs_startup as boolean ?? false}
                onchange={(e) => settings.update((s) => ({ ...s, clear_logs_startup: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Clear Logs on Startup</span>
            </label>
          </div>
        </div>
      </section>

    {:else if activeTab === 'connection'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">Server Connection</h2>

        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Backend</h3>
          <p class="text-xs text-[var(--text-secondary)]">
            {#if $serverUrl}
              Connected to <span class="text-[var(--text-primary)] font-medium">{$serverUrl}</span>.
            {:else}
              Using the same origin this page was loaded from. Set an explicit URL
              for the Android app or to point at a remote server.
            {/if}
          </p>
          <ServerConnection />
        </div>

        <h2 class="text-lg font-semibold pt-2">Security</h2>
        <ChangePassword />
      </section>

    {:else if activeTab === 'plex'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">Plex Integration</h2>

        <!-- Connection Status card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Connection</h3>

          <div class="flex items-center gap-3 p-3 rounded-lg bg-[var(--bg-tertiary)]">
            <div class="w-2.5 h-2.5 rounded-full {$plexConnected ? 'bg-green-500' : 'bg-red-500'}"></div>
            <span class="text-sm">{$plexConnected ? `Connected to ${$plexServer}` : 'Not connected'}</span>
            <button
              onclick={() => refreshPlexStatus()}
              class="ml-auto text-xs text-[var(--accent)] hover:underline"
            >
              Refresh
            </button>
            <button
              onclick={handlePlexConnect}
              disabled={plexConnecting}
              class="px-3 py-1.5 text-xs rounded-lg bg-[var(--accent)] text-white hover:opacity-90 disabled:opacity-50 transition-opacity"
            >
              {plexConnecting ? 'Connecting...' : 'Connect'}
            </button>
          </div>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Plex URL</span>
            <input
              type="text"
              value={$settings.plex_url as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, plex_url: e.currentTarget.value }))}
              placeholder="http://localhost:32400"
              class={inputClass}
            />
          </label>

          <label class="block">
            <span class="flex items-center gap-1.5 text-sm text-[var(--text-secondary)]">
              Plex Token
              {#if testResults['plex'] === 'success'}<span class="w-2 h-2 rounded-full bg-[var(--success)] inline-block" title="Valid"></span>{/if}
              {#if testResults['plex'] === 'error'}<span class="w-2 h-2 rounded-full bg-[var(--error)] inline-block" title="Invalid"></span>{/if}
            </span>
            <div class="flex gap-2 mt-1">
              <input
                type="password"
                value={$settings.plex_token as string ?? ''}
                oninput={(e) => { clearTestResult('plex'); settings.update((s) => ({ ...s, plex_token: e.currentTarget.value })); }}
                class="flex-1 bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
              />
              <button
                onclick={() => testChannel('plex')}
                disabled={testingChannel === 'plex'}
                class={testBtnClassFor('plex')}
              >
                {testBtnLabel('plex')}
              </button>
            </div>
          </label>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.auto_connect_plex as boolean ?? true}
              onchange={(e) => settings.update((s) => ({ ...s, auto_connect_plex: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm">Auto-connect to Plex on startup</span>
          </label>
        </div>

        <!-- Cache Settings card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Cache</h3>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Plex Refresh Mode</span>
            <select
              value={$settings.plex_refresh_mode as string ?? 'auto'}
              onchange={(e) => settings.update((s) => ({ ...s, plex_refresh_mode: e.currentTarget.value }))}
              class={inputClass}
            >
              <option value="auto">Auto (refresh when cache is stale)</option>
              <option value="always">Always refresh before scan</option>
              <option value="never">Never auto-refresh</option>
            </select>
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Cache Duration (hours)</span>
            <input
              type="number"
              min="1"
              max="168"
              value={$settings.cache_duration as number ?? 4}
              oninput={(e) => settings.update((s) => ({ ...s, cache_duration: parseInt(e.currentTarget.value) || 4 }))}
              class={inputSmClass}
            />
          </label>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.plex_invalidate_on_new_content as boolean ?? true}
              onchange={(e) => settings.update((s) => ({ ...s, plex_invalidate_on_new_content: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm">Invalidate cache when new content detected</span>
          </label>

          <div>
            <button
              onclick={async () => {
                plexRefreshing = true;
                try {
                  await api.plexRefresh();
                  addToast('Plex', 'Library cache refresh started');
                } catch (e) {
                  addToast('Plex', e instanceof Error ? e.message : 'Failed to refresh library cache', 'error');
                } finally {
                  plexRefreshing = false;
                }
              }}
              disabled={plexRefreshing || !$plexConnected}
              class="px-4 py-2 text-sm rounded-lg bg-[var(--bg-tertiary)] hover:bg-[var(--border)] text-[var(--text-primary)] border border-[var(--border)] transition-colors disabled:opacity-50 flex items-center gap-2"
            >
              {#if plexRefreshing}
                <svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none">
                  <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="3" stroke-dasharray="31.4 31.4" stroke-linecap="round" />
                </svg>
                Refreshing...
              {:else}
                Refresh Library Cache
              {/if}
            </button>
          </div>
        </div>

        <!-- Library Metadata Scan card -->
        <PlexMetadataScanPanel />

        <!-- Library Assignment card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Library Assignment</h3>
          <p class="text-xs text-[var(--text-secondary)]">Assign your Plex libraries as Movie or TV for matching.</p>

          {#if libraryError}
            <p class="text-xs text-[var(--error)]">{libraryError}</p>
          {:else if knownLibraries.length === 0}
            <p class="text-xs text-[var(--text-secondary)]">No libraries found. Connect to Plex and refresh the library cache.</p>
          {:else}
            <div class="space-y-2">
              {#each knownLibraries as lib}
                <div class="flex items-center gap-4 p-2 rounded bg-[var(--bg-tertiary)]">
                  <span class="text-sm flex-1">{lib}</span>
                  <label class="flex items-center gap-1.5 text-xs">
                    <input
                      type="checkbox"
                      checked={movieLibs.includes(lib)}
                      onchange={() => toggleLibrary(lib, 'movie')}
                      class="accent-[var(--accent)]"
                    />
                    Movie
                  </label>
                  <label class="flex items-center gap-1.5 text-xs">
                    <input
                      type="checkbox"
                      checked={tvLibs.includes(lib)}
                      onchange={() => toggleLibrary(lib, 'tv')}
                      class="accent-[var(--accent)]"
                    />
                    TV
                  </label>
                </div>
              {/each}
            </div>
          {/if}
        </div>
      </section>

    {:else if activeTab === 'sources'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">Scan Sources</h2>
        <p class="text-sm text-[var(--text-secondary)]">Configure API keys and scan source settings.</p>

        <!-- API Keys card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">API Keys</h3>

          <label class="block">
            <span class="flex items-center gap-1.5 text-sm text-[var(--text-secondary)]">
              TMDB API Key
              {#if testResults['tmdb'] === 'success'}<span class="w-2 h-2 rounded-full bg-[var(--success)] inline-block" title="Valid"></span>{/if}
              {#if testResults['tmdb'] === 'error'}<span class="w-2 h-2 rounded-full bg-[var(--error)] inline-block" title="Invalid"></span>{/if}
            </span>
            <div class="flex gap-2 mt-1">
              <input
                type="password"
                value={$settings.tmdb_api_key as string ?? ''}
                oninput={(e) => { clearTestResult('tmdb'); settings.update((s) => ({ ...s, tmdb_api_key: e.currentTarget.value })); }}
                class="flex-1 bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
              />
              <button
                onclick={() => testChannel('tmdb')}
                disabled={testingChannel === 'tmdb'}
                class={testBtnClassFor('tmdb')}
              >
                {testBtnLabel('tmdb')}
              </button>
            </div>
          </label>

          <label class="block">
            <span class="flex items-center gap-1.5 text-sm text-[var(--text-secondary)]">
              OMDb API Key
              {#if testResults['omdb'] === 'success'}<span class="w-2 h-2 rounded-full bg-[var(--success)] inline-block" title="Valid"></span>{/if}
              {#if testResults['omdb'] === 'error'}<span class="w-2 h-2 rounded-full bg-[var(--error)] inline-block" title="Invalid"></span>{/if}
            </span>
            <div class="flex gap-2 mt-1">
              <input
                type="password"
                value={$settings.omdb_api_key as string ?? ''}
                oninput={(e) => { clearTestResult('omdb'); settings.update((s) => ({ ...s, omdb_api_key: e.currentTarget.value })); }}
                class="flex-1 bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
              />
              <button
                onclick={() => testChannel('omdb')}
                disabled={testingChannel === 'omdb'}
                class={testBtnClassFor('omdb')}
              >
                {testBtnLabel('omdb')}
              </button>
            </div>
          </label>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.use_tmdb as boolean ?? true}
              onchange={(e) => settings.update((s) => ({ ...s, use_tmdb: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm">Use TMDB for metadata enrichment</span>
          </label>
        </div>

        <!-- Source Options card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Source Options</h3>

          <div class="space-y-2">
            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.source_2160p as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, source_2160p: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Include 2160p sources</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.source_remux as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, source_remux: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Include Remux sources</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.source_tv_packs as boolean ?? false}
                onchange={(e) => settings.update((s) => ({ ...s, source_tv_packs: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Include TV packs</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.exclude_720p as boolean ?? false}
                onchange={(e) => settings.update((s) => ({ ...s, exclude_720p: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Exclude 720p results</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.ddlbase_enabled as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, ddlbase_enabled: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">DDLBase enabled</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.adithd_enabled as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, adithd_enabled: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">AdiTHD enabled</span>
            </label>
          </div>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Scan Threads</span>
            <input
              type="number"
              min="1"
              max="50"
              value={$settings.scan_threads as number ?? 10}
              oninput={(e) => settings.update((s) => ({ ...s, scan_threads: parseInt(e.currentTarget.value) || 10 }))}
              class={inputSmClass}
            />
          </label>
        </div>

        <!-- AdiTHD Credentials card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">AdiTHD Credentials</h3>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Username</span>
            <input
              type="text"
              value={$settings.adithd_username as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, adithd_username: e.currentTarget.value }))}
              class={inputClass}
            />
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Password</span>
            <input
              type="password"
              value={$settings.adithd_password as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, adithd_password: e.currentTarget.value }))}
              class={inputClass}
            />
          </label>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.adithd_auto_reply as boolean ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, adithd_auto_reply: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm">Auto-reply to threads</span>
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Preferred Host</span>
            <input
              type="text"
              value={$settings.adithd_preferred_host as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, adithd_preferred_host: e.currentTarget.value }))}
              placeholder="e.g. rapidgator, 1fichier"
              class={inputClass}
            />
          </label>
        </div>

        <!-- Cuty Credentials card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Cuty Credentials</h3>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Email</span>
            <input
              type="text"
              value={$settings.cuty_email as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, cuty_email: e.currentTarget.value }))}
              class={inputClass}
            />
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Password</span>
            <input
              type="password"
              value={$settings.cuty_password as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, cuty_password: e.currentTarget.value }))}
              class={inputClass}
            />
          </label>
        </div>

        <!-- JDownloader Integration card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <div class="flex items-center justify-between">
            <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">JDownloader Integration</h3>
            <!-- Connection status indicator -->
            <div class="flex items-center gap-2">
              <span class="flex items-center gap-1.5 text-xs">
                <span class="w-2 h-2 rounded-full {$jdConnection.checking ? 'bg-[var(--warning)] animate-pulse' : $jdConnection.connected ? 'bg-[var(--success)]' : 'bg-[var(--error)]'}"></span>
                <span class="text-[var(--text-secondary)]" title={$jdConnection.error ?? ''}>
                  {#if $jdConnection.checking}Checking…{:else if $jdConnection.connected}{$jdConnection.device || 'Connected'}{:else}Not connected{/if}
                </span>
              </span>
              <button
                onclick={testJd}
                disabled={$jdConnection.checking}
                class="px-2.5 py-1 rounded text-xs font-medium bg-[var(--bg-tertiary)] hover:bg-[var(--border)] transition-colors disabled:opacity-50"
              >
                Test Connection
              </button>
            </div>
          </div>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.jd_enabled as boolean ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, jd_enabled: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm font-medium">Enable JDownloader</span>
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Method</span>
            <select
              value={$settings.jd_method as string ?? 'folder'}
              onchange={(e) => settings.update((s) => ({ ...s, jd_method: e.currentTarget.value }))}
              class={inputClass}
            >
              <option value="folder">Folder Watch</option>
              <option value="api">MyJDownloader API</option>
            </select>
          </label>

          {#if ($settings.jd_method ?? 'folder') === 'folder'}
            <label class="block">
              <span class="text-sm text-[var(--text-secondary)]">Watch Folder Path</span>
              <input
                type="text"
                value={$settings.jd_folder as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, jd_folder: e.currentTarget.value }))}
                placeholder="C:\\JDownloader\\folderwatch"
                class={inputClass}
              />
            </label>
          {:else}
            <label class="block">
              <span class="text-sm text-[var(--text-secondary)]">MyJDownloader Email</span>
              <input
                type="text"
                value={$settings.jd_email as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, jd_email: e.currentTarget.value }))}
                class={inputClass}
              />
            </label>

            <label class="block">
              <span class="text-sm text-[var(--text-secondary)]">MyJDownloader Password</span>
              <input
                type="password"
                value={$settings.jd_password as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, jd_password: e.currentTarget.value }))}
                class={inputClass}
              />
            </label>

            <label class="block">
              <span class="text-sm text-[var(--text-secondary)]">Device Name</span>
              <input
                type="text"
                value={$settings.jd_device as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, jd_device: e.currentTarget.value }))}
                class={inputClass}
              />
            </label>
          {/if}

          <div class="pt-3 mt-1 border-t border-[var(--border)] space-y-3">
            <p class="text-xs text-[var(--text-secondary)]">
              Optional per-type download folders. When set, movie grabs go to the Movies path and TV grabs (items with a season) to the TV path — JDownloader extracts there. Leave blank to use JDownloader's default folder.
            </p>
            <label class="block">
              <span class="text-sm text-[var(--text-secondary)]">Movies Download Folder</span>
              <input
                type="text"
                value={$settings.jd_movies_folder as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, jd_movies_folder: e.currentTarget.value }))}
                placeholder="e.g. /downloads/Movies"
                class={inputClass}
              />
            </label>
            <label class="block">
              <span class="text-sm text-[var(--text-secondary)]">4K Movies Download Folder</span>
              <input
                type="text"
                value={$settings.jd_movies_folder_4k as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, jd_movies_folder_4k: e.currentTarget.value }))}
                placeholder="e.g. G:\Downloads (same drive as the 4K library)"
                class={inputClass}
              />
              <span class="mt-1 block text-[11px] text-[var(--text-secondary)]">
                Point this at a folder on the <strong>same physical drive</strong> as your 4K library so 4K renames are instant moves, not slow cross-drive copies. Add a matching Path Mapping below (host&nbsp;⇒&nbsp;container) so the extract is found.
              </span>
            </label>
            <label class="block">
              <span class="text-sm text-[var(--text-secondary)]">TV Shows Download Folder</span>
              <input
                type="text"
                value={$settings.jd_tv_folder as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, jd_tv_folder: e.currentTarget.value }))}
                placeholder="e.g. /downloads/TV"
                class={inputClass}
              />
            </label>
          </div>
        </div>
      </section>

    {:else if activeTab === 'matching'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">Matching Rules</h2>
        <p class="text-sm text-[var(--text-secondary)]">Configure how titles are matched and upgrade rules.</p>

        <!-- Thresholds card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Thresholds</h3>

          <label class="block">
            <Tooltip text="Minimum title similarity score (0–100) for a scan result to be considered a match against your Plex library. Lower = more matches but more false positives. 85 is a good default.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Movie Match Threshold (%) ⓘ</span>
            </Tooltip>
            <input
              type="number"
              min="50"
              max="100"
              value={$settings.movie_match_threshold as number ?? 85}
              oninput={(e) => settings.update((s) => ({ ...s, movie_match_threshold: parseInt(e.currentTarget.value) || 85 }))}
              class={inputSmClass}
            />
          </label>

          <label class="block">
            <Tooltip text="Same as Movie Match Threshold but for TV show episode matching. Episode title fuzziness and regional date differences make TV titles harder to match, so a slightly lower threshold (e.g. 80) is often appropriate.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">TV Match Threshold (%) ⓘ</span>
            </Tooltip>
            <input
              type="number"
              min="50"
              max="100"
              value={$settings.tv_match_threshold as number ?? 90}
              oninput={(e) => settings.update((s) => ({ ...s, tv_match_threshold: parseInt(e.currentTarget.value) || 90 }))}
              class={inputSmClass}
            />
          </label>

          <label class="block">
            <Tooltip text="Results that score between this number and the Movie/TV threshold are shown as low-confidence matches — visible in results but not acted on automatically. Below this number = no match. Raise it to be stricter about what counts as a partial match.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Low Match Threshold (%) ⓘ</span>
            </Tooltip>
            <input
              type="number"
              min="30"
              max="100"
              value={$settings.low_match_threshold as number ?? 75}
              oninput={(e) => settings.update((s) => ({ ...s, low_match_threshold: parseInt(e.currentTarget.value) || 75 }))}
              class={inputSmClass}
            />
          </label>

          <label class="block">
            <Tooltip text="How many years off a title's release year can be and still count as a match. Set to 1 to allow for regional release date differences (e.g. a 2023 film sometimes listed as 2024).">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Year Tolerance ⓘ</span>
            </Tooltip>
            <input
              type="number"
              min="0"
              max="5"
              value={$settings.year_tolerance as number ?? 1}
              oninput={(e) => settings.update((s) => ({ ...s, year_tolerance: parseInt(e.currentTarget.value) || 1 }))}
              class={inputSmClass}
            />
          </label>

          <label class="block">
            <Tooltip text="How much larger (as a percentage) a new file must be than your existing copy at the same resolution to count as a size upgrade — e.g. 10 means it must be at least 10% larger. Lower flags more upgrades (even minor re-encodes); higher only flags clearly bigger files. A resolution jump (1080p→4K) is always flagged; a file that ADDS Dolby Vision is always flagged; a file that would LOSE Dolby Vision uses the higher threshold below instead.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Upgrade Sensitivity (%) ⓘ</span>
            </Tooltip>
            <input
              type="number"
              min="0"
              max="100"
              value={$settings.upgrade_sensitivity as number ?? 10}
              oninput={(e) => settings.update((s) => ({ ...s, upgrade_sensitivity: parseInt(e.currentTarget.value) || 10 }))}
              class={inputSmClass}
            />
          </label>

          <label class="block">
            <Tooltip text="A higher bar, only for a same-resolution file that is bigger but would DROP Dolby Vision (your copy has DV, the new one doesn't). It must be at least this much larger to still count as an upgrade — otherwise it stays In Library. Set equal to Upgrade Sensitivity to treat DV loss the same as any other size upgrade. Only applies when Dolby Vision upgrades are enabled below.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">DV-loss Upgrade Threshold (%) ⓘ</span>
            </Tooltip>
            <input
              type="number"
              min="0"
              max="100"
              value={$settings.upgrade_dv_loss_sensitivity as number ?? 20}
              oninput={(e) => settings.update((s) => ({ ...s, upgrade_dv_loss_sensitivity: parseInt(e.currentTarget.value) || 20 }))}
              class={inputSmClass}
            />
          </label>
        </div>

        <!-- Upgrade Rules card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Upgrade Rules</h3>

          <div class="space-y-2">
            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.rule_1080_4k as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, rule_1080_4k: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">1080p to 4K upgrades</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.rule_1080_4k_size as boolean ?? false}
                onchange={(e) => settings.update((s) => ({ ...s, rule_1080_4k_size: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">1080p to 4K (size-based)</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.rule_1080_1080 as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, rule_1080_1080: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">1080p to 1080p upgrades</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.rule_4k_4k as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, rule_4k_4k: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">4K to 4K upgrades</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.rule_dv as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, rule_dv: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Dolby Vision upgrades</span>
            </label>

            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.strict_resolution as boolean ?? false}
                onchange={(e) => settings.update((s) => ({ ...s, strict_resolution: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm">Strict resolution matching</span>
            </label>
          </div>
        </div>
      </section>

    {:else if activeTab === 'autograb'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">Auto-Grab</h2>
        <p class="text-sm text-[var(--text-secondary)]">Automatically grab results matching these criteria after a scan.</p>

        <!-- Enable & Criteria card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Criteria</h3>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.auto_grab_enabled as boolean ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, auto_grab_enabled: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm font-medium">Enable Auto-Grab</span>
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Minimum Rating</span>
            <input
              type="number"
              min="0"
              max="10"
              step="0.1"
              value={$settings.auto_grab_min_rating as number ?? 0}
              oninput={(e) => settings.update((s) => ({ ...s, auto_grab_min_rating: parseFloat(e.currentTarget.value) || 0 }))}
              class={inputSmClass}
            />
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Minimum Votes</span>
            <input
              type="number"
              min="0"
              value={$settings.auto_grab_min_votes as number ?? 0}
              oninput={(e) => settings.update((s) => ({ ...s, auto_grab_min_votes: parseInt(e.currentTarget.value) || 0 }))}
              class={inputSmClass}
            />
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Statuses to Grab (comma-separated)</span>
            <input
              type="text"
              value={$settings.auto_grab_statuses as string ?? 'missing,upgrade,dv_upgrade'}
              oninput={(e) => settings.update((s) => ({ ...s, auto_grab_statuses: e.currentTarget.value }))}
              placeholder="missing,upgrade,dv_upgrade"
              class={inputClass}
            />
          </label>
        </div>

        <!-- Genre & Language Filters card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Genre & Language Filters</h3>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Include Genres (comma-separated, empty = all)</span>
            <input
              type="text"
              value={$settings.auto_grab_genres as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, auto_grab_genres: e.currentTarget.value }))}
              placeholder="Action, Thriller, Sci-Fi"
              class={inputClass}
            />
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Exclude Genres (comma-separated)</span>
            <input
              type="text"
              value={$settings.auto_grab_exclude_genres as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, auto_grab_exclude_genres: e.currentTarget.value }))}
              placeholder="Horror, Documentary"
              class={inputClass}
            />
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Languages (comma-separated, empty = all)</span>
            <input
              type="text"
              value={$settings.auto_grab_languages as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, auto_grab_languages: e.currentTarget.value }))}
              placeholder="English, French"
              class={inputClass}
            />
          </label>
        </div>
      </section>

    {:else if activeTab === 'scheduler'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">Scheduler</h2>
        <p class="text-sm text-[var(--text-secondary)]">Configure automatic scheduled scans.</p>

        <!-- Schedule Settings card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Schedule</h3>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.scheduler_enabled as boolean ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, scheduler_enabled: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm font-medium">Enable Scheduler</span>
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Scan Interval (hours)</span>
            <input
              type="number"
              min="1"
              max="168"
              value={$settings.scheduler_interval as number ?? 24}
              oninput={(e) => settings.update((s) => ({ ...s, scheduler_interval: parseInt(e.currentTarget.value) || 24 }))}
              class={inputSmClass}
            />
          </label>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.scheduler_only_when_idle as boolean ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, scheduler_only_when_idle: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm">Only scan when idle</span>
          </label>

          {#if $settings.last_scan_time}
            <div class="text-xs text-[var(--text-secondary)] pt-2">
              Last scan: {new Date(($settings.last_scan_time as number) * 1000).toLocaleString()}
            </div>
          {/if}

          <div class="pt-2">
            <button
              onclick={triggerScan}
              disabled={triggerLoading || $scanState !== 'idle'}
              class="px-4 py-2 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50"
            >
              {triggerLoading ? 'Triggering...' : 'Run Now'}
            </button>
          </div>
        </div>

        <!-- Scheduler Status card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Status</h3>

          {#if schedulerStatus}
            <div class="flex items-center gap-2">
              <span
                class="inline-block w-2.5 h-2.5 rounded-full {schedulerStatus.scheduler_active ? 'bg-green-500' : 'bg-gray-400'}"
              ></span>
              <span class="text-sm font-medium">
                {schedulerStatus.scheduler_active ? 'Scheduler active' : 'Scheduler inactive'}
              </span>
            </div>

            <div class="text-sm text-[var(--text-secondary)]">
              <span class="font-medium text-[var(--text-primary)]">Next run:</span>
              {#if schedulerStatus.next_run}
                {new Date(schedulerStatus.next_run).toLocaleString()}
              {:else}
                Not scheduled
              {/if}
            </div>
          {:else}
            <div class="text-sm text-[var(--text-secondary)]">Loading status...</div>
          {/if}
        </div>
      </section>

    {:else if activeTab === 'background'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">Background Scan</h2>
        <p class="text-sm text-[var(--text-secondary)]">
          Pre-fetch results on a schedule so the app opens with results already populated. Off by default.
        </p>

        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Schedule</h3>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.background_scan_enabled ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, background_scan_enabled: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm font-medium">Enable background scanning</span>
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Interval</span>
            <select
              value={String($settings.background_scan_interval_hours ?? 6)}
              onchange={(e) => settings.update((s) => ({ ...s, background_scan_interval_hours: parseInt(e.currentTarget.value) }))}
              class={inputClass}
            >
              <option value="1">Every 1 hour</option>
              <option value="3">Every 3 hours</option>
              <option value="6">Every 6 hours</option>
              <option value="12">Every 12 hours</option>
              <option value="24">Every 24 hours</option>
            </select>
          </label>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Pages per source</span>
            <input
              type="number"
              min="1"
              max="20"
              value={$settings.background_scan_pages ?? 3}
              oninput={(e) => settings.update((s) => ({ ...s, background_scan_pages: parseInt(e.currentTarget.value) || 3 }))}
              class={inputSmClass}
            />
          </label>

          <div>
            <span class="text-sm text-[var(--text-secondary)]">Sources</span>
            <div class="mt-2 flex flex-wrap gap-4">
              {#each ['HDEncode', 'DDLBase', 'Adit-HD'] as src}
                <label class="flex items-center gap-2">
                  <input
                    type="checkbox"
                    checked={($settings.background_scan_sources ?? []).includes(src)}
                    onchange={(e) => toggleBgSource(src, e.currentTarget.checked)}
                    class="accent-[var(--accent)]"
                  />
                  <span class="text-sm">{src}</span>
                </label>
              {/each}
            </div>
          </div>

          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Retain results for</span>
            <select
              value={String($settings.background_scan_retain_days ?? 7)}
              onchange={(e) => settings.update((s) => ({ ...s, background_scan_retain_days: parseInt(e.currentTarget.value) }))}
              class={inputClass}
            >
              <option value="1">1 day</option>
              <option value="3">3 days</option>
              <option value="7">7 days</option>
              <option value="14">14 days</option>
              <option value="30">30 days</option>
            </select>
          </label>
        </div>

        <!-- Status card -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-3">
          <div class="flex items-center justify-between">
            <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Status</h3>
            <button
              onclick={runBackgroundScan}
              disabled={bgScanning}
              class="px-3 py-1.5 text-xs rounded-lg bg-[var(--accent)] hover:opacity-90 text-white font-medium transition disabled:opacity-50"
            >{bgScanning ? 'Starting…' : 'Scan now'}</button>
          </div>
          {#if backgroundStatus}
            <div class="grid grid-cols-2 gap-y-1 text-sm">
              <span class="text-[var(--text-secondary)]">Cached results</span>
              <span class="text-right font-medium">{backgroundStatus.cached_count}</span>
              <span class="text-[var(--text-secondary)]">Last scan</span>
              <span class="text-right">{backgroundStatus.last_run_at ? new Date(backgroundStatus.last_run_at).toLocaleString() : 'Never'}</span>
              <span class="text-[var(--text-secondary)]">Next scan</span>
              <span class="text-right">{backgroundStatus.enabled && backgroundStatus.next_run_at ? new Date(backgroundStatus.next_run_at).toLocaleString() : 'Not scheduled'}</span>
            </div>
            {#if backgroundStatus.running}
              <p class="text-xs text-[var(--accent)]">A scan is running…</p>
            {/if}
          {:else}
            <div class="text-sm text-[var(--text-secondary)]">Loading status…</div>
          {/if}
        </div>
      </section>

    {:else if activeTab === 'rename'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">Auto-Rename</h2>
        <p class="text-sm text-[var(--text-secondary)]">
          After JDownloader extracts a download, identify it and rename/move it into a
          Plex-friendly library. Tracked in the
          <a href="/renames" class="text-[var(--accent)] hover:underline">Renames</a> tab. Off by default.
        </p>

        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Behaviour</h3>

          <label class="flex items-center gap-3">
            <input type="checkbox" checked={$settings.auto_rename_enabled ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, auto_rename_enabled: e.currentTarget.checked }))}
              class="accent-[var(--accent)]" />
            <span class="text-sm font-medium">Enable auto-rename</span>
          </label>

          <label class="flex items-center gap-3">
            <input type="checkbox" checked={$settings.auto_rename_require_confirmation ?? true}
              onchange={(e) => settings.update((s) => ({ ...s, auto_rename_require_confirmation: e.currentTarget.checked }))}
              class="accent-[var(--accent)]" />
            <span class="text-sm">Require confirmation before moving files (recommended)</span>
          </label>

          <label class="block">
            <Tooltip text="Files identified with a confidence score below this number are held for manual review instead of being renamed automatically. 70 is a safe default — lower it to be more permissive, raise it to be stricter.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Low-confidence threshold ⓘ</span>
            </Tooltip>
            <input type="number" min="0" max="100" value={$settings.auto_rename_confidence_threshold ?? 70}
              oninput={(e) => settings.update((s) => ({ ...s, auto_rename_confidence_threshold: parseInt(e.currentTarget.value) || 70 }))}
              class={inputSmClass} />
          </label>

          <label class="block">
            <Tooltip text="Hardlink: creates a second name for the same file — zero extra disk space, original stays in the download folder. Symlink: a shortcut pointer, breaks if the download folder is moved. Copy: duplicates the file (doubles disk usage). Move: relocates the file and deletes the original from the download folder.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">File placement ⓘ</span>
            </Tooltip>
            <select value={$settings.auto_rename_move_method ?? 'hardlink'}
              onchange={(e) => settings.update((s) => ({ ...s, auto_rename_move_method: e.currentTarget.value }))}
              class={inputClass}>
              <option value="hardlink">Hardlink (keeps original)</option>
              <option value="symlink">Symlink</option>
              <option value="copy">Copy</option>
              <option value="move">Move</option>
            </select>
          </label>

          <label class="flex items-center gap-3">
            <input type="checkbox" checked={$settings.auto_rename_plex_sort_titles ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, auto_rename_plex_sort_titles: e.currentTarget.checked }))}
              class="accent-[var(--accent)]" />
            <span class="text-sm">Compute Plex sort titles (e.g. “Matrix, The”)</span>
          </label>

          <label class="flex items-center gap-3">
            <input type="checkbox" checked={$settings.auto_rename_movie_flat ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, auto_rename_movie_flat: e.currentTarget.checked }))}
              class="accent-[var(--accent)]" />
            <Tooltip text="When on, a single-file movie is placed directly in the library folder (no per-movie subfolder). Split (multi-part) movies still get their own folder. TV shows are unaffected.">
              <span class="text-sm cursor-help underline decoration-dotted">Place movies directly in the library folder ⓘ</span>
            </Tooltip>
          </label>

          <label class="flex items-center gap-3">
            <input type="checkbox" checked={$settings.deletions_require_confirmation ?? true}
              onchange={(e) => settings.update((s) => ({ ...s, deletions_require_confirmation: e.currentTarget.checked }))}
              class="accent-[var(--accent)]" />
            <Tooltip text="When on (default), files are never hard-deleted — cross-device moves send the source to a recoverable trash. Turn off to restore permanent deletes.">
              <span class="text-sm cursor-help underline decoration-dotted">Require confirmation before permanent deletes ⓘ</span>
            </Tooltip>
          </label>

          <label class="block">
            <Tooltip text="How many days a file stays in the recoverable trash before it's permanently swept. Applies to every per-volume trash location, checked hourly. 30 is a safe default.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Trash retention (days) ⓘ</span>
            </Tooltip>
            <input type="number" min="1" max="365" value={$settings.trash_retention_days ?? 30}
              oninput={(e) => settings.update((s) => ({ ...s, trash_retention_days: parseInt(e.currentTarget.value) || 30 }))}
              class={inputSmClass} />
          </label>
        </div>

        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Library destinations</h3>
          <label class="block">
            <Tooltip text="Path inside the container where 1080p (and other non-4K) movies are placed. Example: /library/Movies. Must be a volume-mounted path the container can write to.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Movies folder (1080p) ⓘ</span>
            </Tooltip>
            <input type="text" value={$settings.auto_rename_movie_library ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, auto_rename_movie_library: e.currentTarget.value }))}
              placeholder="/library/Movies" class={inputClass} />
          </label>
          <label class="block">
            <Tooltip text="Path for 4K / 2160p movies. When a renamed file is identified as 2160p, it goes here instead of the Movies folder above. Leave blank to put all movies in one folder regardless of resolution.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Movies folder (4K) ⓘ</span>
            </Tooltip>
            <input type="text" value={$settings.auto_rename_movie_library_4k ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, auto_rename_movie_library_4k: e.currentTarget.value }))}
              placeholder="/library/Movies (4K)" class={inputClass} />
          </label>
          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">TV folder</span>
            <input type="text" value={$settings.auto_rename_tv_library ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, auto_rename_tv_library: e.currentTarget.value }))}
              placeholder="/library/TV" class={inputClass} />
          </label>
          <label class="block">
            <Tooltip text={'JDownloader runs on the host and reports Windows paths (e.g. F:\\Downloads\\Movie), but ScanHound runs in a container that sees those folders bind-mounted at a different path. Map each host download folder to its container path, one per line, as: host => container. Example: F:\\Downloads => /library/movies. Update this if JDownloader’s download folder changes (and add a matching volume mount in docker-compose).'}>
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Download path mappings (host ⇒ container) ⓘ</span>
            </Tooltip>
            <textarea rows="2" value={$settings.auto_rename_path_mappings ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, auto_rename_path_mappings: e.currentTarget.value }))}
              placeholder={'F:\\Downloads => /library/movies'} class="{inputClass} font-mono text-xs"></textarea>
          </label>
          <p class="text-xs text-[var(--text-secondary)]">Leave the templates blank for the Plex default naming convention.</p>
          <label class="block">
            <Tooltip text={'Tokens: {{title}} {{year}} {{resolution}} {{imdb_id}} {{tmdb_id}}. Sections in [ ] are omitted when the token is empty. Default (blank): Title (Year) [resolution].mkv'}>
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Movie name template (optional) ⓘ</span>
            </Tooltip>
            <input type="text" value={$settings.auto_rename_template_movie ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, auto_rename_template_movie: e.currentTarget.value }))}
              placeholder={'{{title}} ({{year}}) [{{resolution}}]'} class={inputClass} />
          </label>
          <label class="block">
            <Tooltip text={'Tokens: {{title}} {{year}} {{season}} {{episode}} {{episode_title}} {{resolution}}. Sections in [ ] are omitted when empty. Default (blank): Show (Year) - S01E01 - Episode Title.mkv'}>
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">TV name template (optional) ⓘ</span>
            </Tooltip>
            <input type="text" value={$settings.auto_rename_template_tv ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, auto_rename_template_tv: e.currentTarget.value }))}
              placeholder={'{{title}} - S{{season}}E{{episode}}[ - {{episode_title}}]'} class={inputClass} />
          </label>
        </div>

        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
          <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">Ollama assist (optional)</h3>
          <p class="text-xs text-[var(--text-secondary)]">
            Use a local Ollama model to help identify messy filenames when confidence is low.
            TMDB still confirms every match.
          </p>
          <label class="flex items-center gap-3">
            <input type="checkbox" checked={$settings.auto_rename_llm_enabled ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, auto_rename_llm_enabled: e.currentTarget.checked }))}
              class="accent-[var(--accent)]" />
            <span class="text-sm">Enable Ollama-assisted identification</span>
          </label>
          <label class="block">
            <Tooltip text="URL of your Ollama instance. If Ollama runs as a Docker container on the same proxy network use its container name, e.g. http://ollama:11434. For Ollama installed directly on the host use http://host.docker.internal:11434.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Ollama URL ⓘ</span>
            </Tooltip>
            <input type="text" value={$settings.ollama_base_url ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, ollama_base_url: e.currentTarget.value }))}
              placeholder="http://ollama:11434" class={inputClass} />
          </label>
          <label class="block">
            <span class="text-sm text-[var(--text-secondary)]">Model</span>
            <input type="text" value={$settings.ollama_model ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, ollama_model: e.currentTarget.value }))}
              placeholder="llama3.1:8b" class={inputClass} />
          </label>
          <label class="block">
            <Tooltip text="Vision-capable model used ONLY for the frame-identification fallback (reading title cards / credits from extracted video frames). Must be a multimodal model, e.g. minicpm-v:latest — a text-only model like the one above cannot read images. If left blank, the vision fallback is skipped entirely.">
              <span class="text-sm text-[var(--text-secondary)] cursor-help underline decoration-dotted">Vision model ⓘ</span>
            </Tooltip>
            <input type="text" value={$settings.ollama_vision_model ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, ollama_vision_model: e.currentTarget.value }))}
              placeholder="minicpm-v:latest" class={inputClass} />
          </label>
          <div class="flex items-center gap-2">
            <button onclick={testOllamaConnection} disabled={ollamaTesting} class={testBtnClass}>
              {ollamaTesting ? 'Testing…' : 'Test connection'}
            </button>
            {#if ollamaTest}
              <span class="text-xs {ollamaTest.ok ? 'text-[var(--success)]' : 'text-[var(--error)]'}">
                {ollamaTest.ok ? `✓ ${ollamaTest.models?.length ?? 0} model(s)` : `✕ ${ollamaTest.error}`}
              </span>
            {/if}
          </div>
        </div>

        <div class="mt-6 pt-4 border-t border-[var(--border)]">
          <h3 class="text-sm font-semibold mb-1">Dolby Vision</h3>
          <p class="text-xs text-[var(--text-secondary)] mb-3">
            Host-side FEL/MEL detection feeding per-copy Plex labels (DV FEL / DV MEL / DV P8 / DV P5) for Kometa badges.
          </p>

          <label class="flex items-center gap-3">
            <input type="checkbox" checked={$settings.dv_detection ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, dv_detection: e.currentTarget.checked }))}
              class="accent-[var(--accent)]" />
            <span class="text-sm font-medium">Enable Dolby Vision detection</span>
          </label>

          <label class="flex items-center gap-3 mt-3">
            <input type="checkbox" checked={$settings.dv_file_tagging ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, dv_file_tagging: e.currentTarget.checked }))}
              class="accent-[var(--accent)]" />
            <span class="text-sm font-medium">Tag MKV track name with the detected layer</span>
          </label>

          <label class="block mt-3">
            <span class="text-sm text-[var(--text-secondary)]">Library roots (host-native, one per line)</span>
            <textarea rows="3" value={$settings.dv_library_roots ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, dv_library_roots: e.currentTarget.value }))}
              placeholder={'Y:\\Movies\nE:\\4K\n\\\\TURTLELANDSRV2\\Share\\Movies'}
              class={inputClass + ' font-mono'}></textarea>
          </label>

          <label class="block mt-3">
            <span class="text-sm text-[var(--text-secondary)]">Label vocabulary (JSON: layer → label)</span>
            <input type="text" value={$settings.dv_label_vocab ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, dv_label_vocab: e.currentTarget.value }))}
              placeholder={'{"fel":"DV FEL","mel":"DV MEL","profile8":"DV P8","profile5":"DV P5"}'}
              class={inputClass + ' font-mono'} />
          </label>
        </div>
      </section>

    {:else if activeTab === 'notifications'}
      <section class="space-y-4">
        <h2 class="text-lg font-semibold">Notifications</h2>

        <!-- Desktop Notifications -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-3">
          <div class="flex items-center justify-between">
            <label class="flex items-center gap-3">
              <input
                type="checkbox"
                checked={$settings.desktop_notifications as boolean ?? true}
                onchange={(e) => settings.update((s) => ({ ...s, desktop_notifications: e.currentTarget.checked }))}
                class="accent-[var(--accent)]"
              />
              <span class="text-sm font-medium">Desktop Notifications</span>
            </label>
            <button
              onclick={() => testChannel('desktop')}
              disabled={testingChannel === 'desktop'}
              class={testBtnClassFor('desktop')}
            >
              {testBtnLabel('desktop')}
            </button>
          </div>
        </div>

        <!-- Discord -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-3">
          <h3 class="text-sm font-semibold">Discord Webhook</h3>

          <label class="block">
            <span class="text-xs text-[var(--text-secondary)]">Webhook URL</span>
            <input
              type="password"
              value={$settings.discord_webhook as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, discord_webhook: e.currentTarget.value }))}
              placeholder="https://discord.com/api/webhooks/..."
              class={inputClass}
            />
          </label>

          <label class="block">
            <span class="text-xs text-[var(--text-secondary)]">Bot Username</span>
            <input
              type="text"
              value={$settings.discord_username as string ?? 'ScanHound'}
              oninput={(e) => settings.update((s) => ({ ...s, discord_username: e.currentTarget.value }))}
              class={inputClass}
            />
          </label>

          <div class="flex justify-end">
            <button
              onclick={() => testChannel('discord')}
              disabled={testingChannel === 'discord'}
              class={testBtnClassFor('discord')}
            >
              {testBtnLabel('discord', 'Discord')}
            </button>
          </div>
        </div>

        <!-- Slack -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-3">
          <h3 class="text-sm font-semibold">Slack Webhook</h3>

          <label class="block">
            <span class="text-xs text-[var(--text-secondary)]">Webhook URL</span>
            <input
              type="password"
              value={$settings.slack_webhook as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, slack_webhook: e.currentTarget.value }))}
              placeholder="https://hooks.slack.com/services/..."
              class={inputClass}
            />
          </label>

          <div class="flex justify-end">
            <button
              onclick={() => testChannel('slack')}
              disabled={testingChannel === 'slack'}
              class={testBtnClassFor('slack')}
            >
              {testBtnLabel('slack', 'Slack')}
            </button>
          </div>
        </div>

        <!-- Pushover -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-3">
          <h3 class="text-sm font-semibold">Pushover</h3>

          <label class="block">
            <span class="text-xs text-[var(--text-secondary)]">User Key</span>
            <input
              type="text"
              value={$settings.pushover_user as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, pushover_user: e.currentTarget.value }))}
              class={inputClass}
            />
          </label>

          <label class="block">
            <span class="text-xs text-[var(--text-secondary)]">API Token</span>
            <input
              type="password"
              value={$settings.pushover_token as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, pushover_token: e.currentTarget.value }))}
              class={inputClass}
            />
          </label>

          <div class="flex justify-end">
            <button
              onclick={() => testChannel('pushover')}
              disabled={testingChannel === 'pushover'}
              class={testBtnClassFor('pushover')}
            >
              {testBtnLabel('pushover', 'Pushover')}
            </button>
          </div>
        </div>

        <!-- Custom Webhook -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-3">
          <h3 class="text-sm font-semibold">Custom Webhook</h3>

          <label class="block">
            <span class="text-xs text-[var(--text-secondary)]">URL</span>
            <input
              type="text"
              value={$settings.webhook_url as string ?? ''}
              oninput={(e) => settings.update((s) => ({ ...s, webhook_url: e.currentTarget.value }))}
              placeholder="https://example.com/webhook"
              class={inputClass}
            />
          </label>

          <label class="block">
            <span class="text-xs text-[var(--text-secondary)]">Method</span>
            <select
              value={$settings.webhook_method as string ?? 'POST'}
              onchange={(e) => settings.update((s) => ({ ...s, webhook_method: e.currentTarget.value }))}
              class={inputClass}
            >
              <option value="POST">POST</option>
              <option value="GET">GET</option>
              <option value="PUT">PUT</option>
            </select>
          </label>

          <div class="flex justify-end">
            <button
              onclick={() => testChannel('webhook')}
              disabled={testingChannel === 'webhook'}
              class={testBtnClassFor('webhook')}
            >
              {testBtnLabel('webhook', 'Webhook')}
            </button>
          </div>
        </div>

        <!-- Email (SMTP) -->
        <div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-3">
          <div class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.email_enabled as boolean ?? false}
              onchange={(e) => settings.update((s) => ({ ...s, email_enabled: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <h3 class="text-sm font-semibold">Email (SMTP)</h3>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <label class="block">
              <span class="text-xs text-[var(--text-secondary)]">SMTP Host</span>
              <input
                type="text"
                value={$settings.smtp_host as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, smtp_host: e.currentTarget.value }))}
                placeholder="smtp.gmail.com"
                class={inputClass}
              />
            </label>

            <label class="block">
              <span class="text-xs text-[var(--text-secondary)]">SMTP Port</span>
              <input
                type="number"
                value={$settings.smtp_port as number ?? 587}
                oninput={(e) => settings.update((s) => ({ ...s, smtp_port: parseInt(e.currentTarget.value) || 587 }))}
                class={inputClass}
              />
            </label>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <label class="block">
              <span class="text-xs text-[var(--text-secondary)]">Username</span>
              <input
                type="text"
                value={$settings.smtp_username as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, smtp_username: e.currentTarget.value }))}
                class={inputClass}
              />
            </label>

            <label class="block">
              <span class="text-xs text-[var(--text-secondary)]">Password</span>
              <input
                type="password"
                value={$settings.smtp_password as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, smtp_password: e.currentTarget.value }))}
                class={inputClass}
              />
            </label>
          </div>

          <div class="grid grid-cols-2 gap-3">
            <label class="block">
              <span class="text-xs text-[var(--text-secondary)]">From Address</span>
              <input
                type="email"
                value={$settings.email_from as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, email_from: e.currentTarget.value }))}
                class={inputClass}
              />
            </label>

            <label class="block">
              <span class="text-xs text-[var(--text-secondary)]">To Address</span>
              <input
                type="email"
                value={$settings.email_to as string ?? ''}
                oninput={(e) => settings.update((s) => ({ ...s, email_to: e.currentTarget.value }))}
                class={inputClass}
              />
            </label>
          </div>

          <label class="flex items-center gap-3">
            <input
              type="checkbox"
              checked={$settings.smtp_tls as boolean ?? true}
              onchange={(e) => settings.update((s) => ({ ...s, smtp_tls: e.currentTarget.checked }))}
              class="accent-[var(--accent)]"
            />
            <span class="text-sm">Use TLS</span>
          </label>

          <div class="flex justify-end">
            <button
              onclick={() => testChannel('email')}
              disabled={testingChannel === 'email'}
              class={testBtnClassFor('email')}
            >
              {testBtnLabel('email', 'Email')}
            </button>
          </div>
        </div>
      </section>
    {/if}
  </div>
</div>

{#if $isDirty}
  <div class="sticky bottom-0 z-10 flex gap-3 p-4 border-t border-[var(--border)] bg-[var(--bg-secondary)] shadow-lg">
    <button
      onclick={async () => { saving = true; try { await saveSettings(); } finally { saving = false; } }}
      disabled={saving}
      class="px-4 py-2 bg-[var(--accent)] hover:bg-[var(--accent-hover)] text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {saving ? 'Saving...' : 'Save Changes'}
    </button>
    <button
      onclick={() => resetSettings()}
      disabled={saving}
      class="px-4 py-2 bg-[var(--bg-tertiary)] hover:bg-[var(--border)] text-[var(--text-primary)] rounded-lg text-sm transition-colors disabled:opacity-50"
    >
      Discard
    </button>
  </div>
{/if}
