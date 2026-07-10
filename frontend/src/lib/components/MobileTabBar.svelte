<script lang="ts">
  import { page } from '$app/stores';
  import { navItems, navIcon } from '$lib/icons';
</script>

<!-- Phone navigation: fixed, thumb-reachable bottom tab bar. Hidden on md+. -->
<nav
  class="flex md:hidden shrink-0 border-t border-[var(--border)] bg-[var(--bg-secondary)]"
  style="padding-bottom: env(safe-area-inset-bottom);"
  aria-label="Primary"
>
  {#each navItems.filter((n) => !n.desktopOnly) as nav}
    {@const active = $page.url.pathname === nav.href}
    <a
      href={nav.href}
      class="flex-1 flex flex-col items-center justify-center gap-0.5 py-2 min-h-[3.25rem] text-[10px] transition-colors
        {active ? 'text-[var(--accent)]' : 'text-[var(--text-secondary)] active:bg-[var(--bg-tertiary)]'}"
      aria-current={active ? 'page' : undefined}
    >
      <span class="w-5 h-5 flex items-center justify-center">{@html navIcon(nav.icon)}</span>
      <span>{nav.short}</span>
    </a>
  {/each}
</nav>
