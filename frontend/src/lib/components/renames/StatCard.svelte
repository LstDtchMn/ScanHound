<script lang="ts">
  import { renameStatusBorderColor } from '$lib/constants';
  import type { BadgeVariant } from '$lib/components/Badge.svelte';

  let {
    label,
    count,
    variant,
    active = false,
    borderStatus = null,
    onclick
  }: {
    label: string;
    count: number;
    variant: BadgeVariant;
    active?: boolean;
    borderStatus?: string | null;
    onclick: () => void;
  } = $props();

  const tints: Record<BadgeVariant, string> = {
    default: 'var(--border)',
    success: 'var(--success)',
    warning: 'var(--warning)',
    error: 'var(--error)',
    accent: 'var(--accent)',
    info: '#3b82f6',
    orange: '#f97316'
  };
  let color = $derived(borderStatus ? renameStatusBorderColor(borderStatus) : tints[variant]);
</script>

<button
  {onclick}
  aria-pressed={active}
  class="flex-1 min-w-0 text-left rounded-lg border-2 px-3 py-2 transition-colors hover:bg-[var(--bg-tertiary)]/40"
  style="border-color: {active ? color : 'var(--border)'}"
>
  <div class="text-2xl font-bold" style="color: {color}">{count}</div>
  <div class="text-xs text-[var(--text-secondary)] truncate">{label}</div>
</button>
