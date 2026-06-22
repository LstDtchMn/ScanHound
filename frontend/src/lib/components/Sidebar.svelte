<script lang="ts">
  import { page } from '$app/stores';
  import { connection } from '$lib/stores/connection';
  import { plexConnected, plexMovieCount, plexTvCount } from '$lib/stores/plex';
  import { navItems, navIcon } from '$lib/icons';
  import { theme, toggleTheme } from '$lib/stores/theme';
  import { fly } from 'svelte/transition';

  interface Props {
    mobile?: boolean;
    onnavigate?: () => void;
  }
  let { mobile = false, onnavigate }: Props = $props();

  const connectionState = connection.state;
  let isDark = $derived($theme === 'dark');

  function handleNav() {
    onnavigate?.();
  }
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
