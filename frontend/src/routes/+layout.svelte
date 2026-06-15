<script lang="ts">
  import '../app.css';
  import Sidebar from '$lib/components/Sidebar.svelte';
  import Snackbar from '$lib/components/Snackbar.svelte';
  import LogPanel from '$lib/components/LogPanel.svelte';
  import ShortcutsHelp from '$lib/components/ShortcutsHelp.svelte';
  import ConnectionBanner from '$lib/components/ConnectionBanner.svelte';
  import { connection } from '$lib/stores/connection';
  import { logPanelOpen } from '$lib/stores/logs';
  import { viewMode, selectAll, deselectAll } from '$lib/stores/results';
  import { setAuthNonce } from '$lib/api/client';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { fade } from 'svelte/transition';
  import { page } from '$app/stores';

  let { children } = $props();
  let showShortcuts = $state(false);
  let mobileMenuOpen = $state(false);
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
    initAuth().then(() => connection.connect());
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
    <div class="flex md:hidden items-center h-12 px-3 border-b border-[var(--border)] bg-[var(--bg-secondary)]">
      <button
        onclick={() => mobileMenuOpen = true}
        class="p-1.5 rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        aria-label="Open menu"
      >
        <svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
          <path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>
      <span class="ml-2 text-sm font-semibold text-[var(--accent)]">ScanHound</span>
    </div>
    <ConnectionBanner />
    {#key $page.url.pathname}
      <main id="main-content" class="flex-1 flex flex-col overflow-hidden" in:fade={{ duration: 150 }}>
        {@render children()}
      </main>
    {/key}
    <LogPanel />
  </div>
</div>

{#if showShortcuts}
  <ShortcutsHelp onclose={() => showShortcuts = false} />
{/if}

<Snackbar />

{#if mobileMenuOpen}
  <Sidebar mobile onnavigate={() => mobileMenuOpen = false} />
{/if}
