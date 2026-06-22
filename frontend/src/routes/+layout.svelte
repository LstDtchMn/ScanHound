<script lang="ts">
  import '../app.css';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import MobileTabBar from '$lib/components/MobileTabBar.svelte';
  import Snackbar from '$lib/components/Snackbar.svelte';
  import LogPanel from '$lib/components/LogPanel.svelte';
  import ShortcutsHelp from '$lib/components/ShortcutsHelp.svelte';
  import ConnectionBanner from '$lib/components/ConnectionBanner.svelte';
  import ServerConnection from '$lib/components/ServerConnection.svelte';
  import { theme, toggleTheme, initTheme } from '$lib/stores/theme';
  import { connection } from '$lib/stores/connection';
  import { hasRemoteServer } from '$lib/stores/server';
  import { logPanelOpen } from '$lib/stores/logs';
  import { viewMode, selectAll, deselectAll } from '$lib/stores/results';
  import { setAuthNonce, api } from '$lib/api/client';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { fade } from 'svelte/transition';
  import { page } from '$app/stores';

  let { children } = $props();
  let showShortcuts = $state(false);
  let showServerSetup = $state(false);
  let isDark = $derived($theme === 'dark');

  // On a packaged app (Android/desktop) with no remote server configured, the
  // bundled frontend isn't same-origin with any backend. If the initial health
  // check fails, prompt for a server URL + token. Desktop's Python sidecar
  // answers health, so the prompt only appears where it's actually needed.
  async function maybePromptServer() {
    const isTauri = typeof window !== 'undefined' && '__TAURI__' in window;
    if (!isTauri || $hasRemoteServer) return;
    await new Promise((r) => setTimeout(r, 3000)); // give a sidecar time to boot
    try {
      await api.health();
    } catch {
      showServerSetup = true;
    }
  }
  const routeTitles: Record<string, string> = {
    '/': 'Scan',
    '/downloads': 'Downloads',
    '/watchlist': 'Watchlist',
    '/analytics': 'Analytics',
    '/settings': 'Settings'
  };
  let pageTitle = $derived(`${routeTitles[$page.url.pathname] || 'App'} | ScanHound`);

  function handleKeydown(e: KeyboardEvent) {
    // Ignore shortcuts when typing in inputs
    const tag = (e.target as HTMLElement)?.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;

    if (e.ctrlKey || e.metaKey) {
      switch (e.key) {
        case 'l': e.preventDefault(); logPanelOpen.update(v => !v); break;
        case 'a': e.preventDefault(); selectAll(); break;
        case 'd': e.preventDefault(); deselectAll(); break;
      }
    } else {
      switch (e.key) {
        case '1': goto('/'); break;
        case '2': goto('/downloads'); break;
        case '3': goto('/watchlist'); break;
        case '4': goto('/analytics'); break;
        case '5': goto('/settings'); break;
        case 'g': viewMode.set('grid'); break;
        case 'l': viewMode.set('list'); break;
        case '?': showShortcuts = !showShortcuts; break;
      }
    }
  }

  async function initAuth() {
    if (typeof window !== 'undefined' && '__TAURI__' in window) {
      try {
        const { invoke } = await import('@tauri-apps/api/core');
        const nonce: string = await invoke('get_auth_nonce');
        if (nonce) setAuthNonce(nonce);
        const { listen } = await import('@tauri-apps/api/event');
        listen<string>('sidecar-auth-nonce', (event) => {
          if (event.payload) setAuthNonce(event.payload);
        });
      } catch {
        // Not in Tauri context — dev mode, no auth
      }
    }
  }

  onMount(() => {
    initTheme();
    initAuth().then(() => {
      connection.connect();
      maybePromptServer();
    });
    window.addEventListener('keydown', handleKeydown);
    return () => {
      connection.disconnect();
      window.removeEventListener('keydown', handleKeydown);
    };
  });
</script>

<svelte:head>
  <title>{pageTitle}</title>
</svelte:head>

<a href="#main-content" class="sr-only">Skip to content</a>
<div class="flex h-screen bg-[var(--bg-primary)] text-[var(--text-primary)]">
  <Sidebar />
  <div class="flex-1 flex flex-col overflow-hidden">
    <!-- Mobile top bar (visible below md) -->
    <div
      class="flex md:hidden items-center h-12 px-3 border-b border-[var(--border)] bg-[var(--bg-secondary)]"
      style="padding-top: env(safe-area-inset-top);"
    >
      <span class="text-sm font-semibold text-[var(--accent)]">ScanHound</span>
      <div class="flex-1"></div>
      <button
        onclick={toggleTheme}
        class="p-1.5 rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      >
        {#if isDark}
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" /></svg>
        {:else}
          <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" /></svg>
        {/if}
      </button>
    </div>
    <ConnectionBanner />
    {#key $page.url.pathname}
      <main id="main-content" class="flex-1 flex flex-col overflow-hidden min-h-0" in:fade={{ duration: 150 }}>
        {@render children()}
      </main>
    {/key}
    <LogPanel />
    <MobileTabBar />
  </div>
</div>

{#if showShortcuts}
  <ShortcutsHelp onclose={() => showShortcuts = false} />
{/if}

{#if showServerSetup}
  <div class="fixed inset-0 z-50 flex items-center justify-center bg-[var(--bg-overlay)] p-4">
    <div class="w-full max-w-md bg-[var(--bg-secondary)] border border-[var(--border)] rounded-2xl shadow-2xl p-5">
      <div class="flex items-center justify-between mb-1">
        <h2 class="text-base font-bold text-[var(--text-primary)]">Connect to your ScanHound server</h2>
        <button onclick={() => (showServerSetup = false)} aria-label="Close" class="p-1 text-[var(--text-secondary)] hover:text-[var(--text-primary)]">&times;</button>
      </div>
      <p class="text-xs text-[var(--text-secondary)] mb-4">Enter the address of your ScanHound backend (the Docker container) and its auth token.</p>
      <ServerConnection onsaved={() => (showServerSetup = false)} />
    </div>
  </div>
{/if}

<Snackbar />
