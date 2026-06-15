<script lang="ts">
  import { page } from '$app/stores';
  import { connection } from '$lib/stores/connection';
  import { plexConnected, plexMovieCount, plexTvCount } from '$lib/stores/plex';
  import { onMount } from 'svelte';
  import { fly } from 'svelte/transition';

  interface Props {
    mobile?: boolean;
    onnavigate?: () => void;
  }
  let { mobile = false, onnavigate }: Props = $props();

  const connectionState = connection.state;
  const THEME_KEY = 'scanhound-theme';
  let isDark = $state(true);

  function applyTheme(theme: 'dark' | 'light') {
    isDark = theme === 'dark';
    if (isDark) {
      document.documentElement.removeAttribute('data-theme');
    } else {
      document.documentElement.setAttribute('data-theme', 'light');
    }
    localStorage.setItem(THEME_KEY, theme);
  }

  onMount(() => {
    const savedTheme = localStorage.getItem(THEME_KEY);
    if (savedTheme === 'light' || savedTheme === 'dark') {
      applyTheme(savedTheme);
    } else {
      const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
      applyTheme(prefersDark ? 'dark' : 'light');
    }
  });

  function toggleTheme() {
    applyTheme(isDark ? 'light' : 'dark');
  }

  function handleNav() {
    onnavigate?.();
  }

  const navItems = [
    { href: '/', label: 'Scan', short: 'Scan', icon: 'search' },
    { href: '/downloads', label: 'Downloads', short: 'DLs', icon: 'download' },
    { href: '/watchlist', label: 'Watchlist', short: 'Watch', icon: 'bookmark' },
    { href: '/analytics', label: 'Analytics', short: 'Stats', icon: 'chart' },
    { href: '/settings', label: 'Settings', short: 'Settings', icon: 'gear' }
  ] as const;
</script>

{#if mobile}
  <!-- Mobile: slide-in overlay -->
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="fixed inset-0 z-40 bg-[var(--bg-overlay)]" onclick={handleNav}></div>
  <nav
    transition:fly={{ x: -256, duration: 200 }}
    class="fixed inset-y-0 left-0 z-50 flex flex-col w-56 bg-[var(--bg-secondary)] border-r border-[var(--border)] shadow-2xl"
  >
    <div class="flex items-center justify-between h-14 px-4 border-b border-[var(--border)]">
      <span class="text-[var(--accent)] font-bold text-lg tracking-tight">ScanHound</span>
      <button
        onclick={handleNav}
        class="p-1.5 rounded-lg text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        aria-label="Close menu"
      >&times;</button>
    </div>

    <div class="flex flex-col gap-1 p-3 flex-1">
      {#each navItems as nav}
        <a
          href={nav.href}
          title={nav.label}
          onclick={handleNav}
          class="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-colors
            {$page.url.pathname === nav.href
              ? 'text-[var(--accent)] bg-[var(--accent)]/5 font-medium'
              : 'text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'}"
        >
          <span class="w-5 h-5 flex items-center justify-center">
            {@html navIcon(nav.icon)}
          </span>
          {nav.label}
        </a>
      {/each}
    </div>

    <div class="p-3 border-t border-[var(--border)] flex items-center justify-between">
      <button
        onclick={toggleTheme}
        class="p-1.5 rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      >
        {@html isDark ? sunIcon : moonIcon}
      </button>
      <div class="flex items-center gap-2">
        <div
          class="w-2 h-2 rounded-full
            {$connectionState === 'connected' ? 'bg-[var(--success)]' :
             $connectionState === 'connecting' ? 'bg-[var(--warning)] animate-pulse' :
             'bg-[var(--error)]'}"
        ></div>
        {#if $plexConnected}
          <span class="text-xs text-[var(--success)]" title="{$plexMovieCount} movies, {$plexTvCount} TV seasons">Plex</span>
        {:else}
          <span class="text-xs text-[var(--warning)] plex-disconnected">Plex</span>
        {/if}
      </div>
    </div>
  </nav>
{:else}
  <!-- Desktop: fixed narrow sidebar -->
  <nav class="hidden md:flex flex-col w-16 h-full bg-[var(--bg-secondary)] border-r border-[var(--border)]">
    <div class="flex items-center justify-center h-14 border-b border-[var(--border)]">
      <span class="text-[var(--accent)] font-bold text-lg tracking-tight transition-all hover:drop-shadow-[0_0_8px_var(--accent)]">SH</span>
    </div>

    <div class="flex flex-col gap-1 p-2 flex-1">
      {#each navItems as nav}
        <a
          href={nav.href}
          title={nav.label}
          class="flex flex-col items-center gap-0.5 p-2 rounded-lg text-[10px] transition-colors
            {$page.url.pathname === nav.href
              ? 'text-[var(--accent)] border-l-[3px] border-[var(--accent)] bg-[var(--accent)]/5'
              : 'border-l-[3px] border-transparent text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)]'}"
        >
          <span class="w-5 h-5 flex items-center justify-center">
            {@html navIcon(nav.icon)}
          </span>
          <span>{nav.short}</span>
        </a>
      {/each}
    </div>

    <div class="p-2 border-t border-[var(--border)] flex flex-col items-center gap-2">
      <button
        onclick={toggleTheme}
        class="p-1.5 rounded-lg text-[var(--text-secondary)] hover:text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] transition-colors"
        title={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      >
        {@html isDark ? sunIcon : moonIcon}
      </button>

      <div class="flex flex-col items-center gap-1">
        <div
          class="w-2 h-2 rounded-full
            {$connectionState === 'connected' ? 'bg-[var(--success)]' :
             $connectionState === 'connecting' ? 'bg-[var(--warning)] animate-pulse' :
             'bg-[var(--error)]'}"
        ></div>
        {#if $plexConnected}
          <span
            class="text-[8px] text-[var(--success)]"
            title="{$plexMovieCount} movies, {$plexTvCount} TV seasons"
          >Plex</span>
        {:else}
          <span class="text-[8px] text-[var(--warning)]">
            <span class="plex-disconnected">Plex</span>
          </span>
        {/if}
      </div>
    </div>
  </nav>
{/if}

<script lang="ts" module>
  const sunIcon = `<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" /></svg>`;
  const moonIcon = `<svg class="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" /></svg>`;

  function navIcon(name: string): string {
    switch (name) {
      case 'search': return `<svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" /></svg>`;
      case 'download': return `<svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" /></svg>`;
      case 'bookmark': return `<svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M5 5a2 2 0 012-2h10a2 2 0 012 2v16l-7-3.5L5 21V5z" /></svg>`;
      case 'chart': return `<svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" /></svg>`;
      case 'gear': return `<svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" /><path stroke-linecap="round" stroke-linejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" /></svg>`;
      default: return '';
    }
  }
</script>

<style>
  .plex-disconnected {
    animation: pulse-amber 2s ease-in-out infinite;
  }

  @keyframes pulse-amber {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }
</style>
