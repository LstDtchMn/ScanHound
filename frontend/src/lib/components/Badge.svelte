<script lang="ts" module>
  export type BadgeVariant = 'default' | 'success' | 'warning' | 'error' | 'accent';
</script>

<script lang="ts">
  interface Props {
    label: string;
    variant?: BadgeVariant;
  }
  let { label, variant = 'default' }: Props = $props();

  const colors: Record<string, string> = {
    default: 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]',
    success: 'bg-[color-mix(in_srgb,var(--success)_20%,var(--bg-secondary))] text-[var(--success)]',
    warning: 'bg-[color-mix(in_srgb,var(--warning)_20%,var(--bg-secondary))] text-[var(--warning)]',
    error: 'bg-[color-mix(in_srgb,var(--error)_20%,var(--bg-secondary))] text-[var(--error)]',
    accent: 'bg-[color-mix(in_srgb,var(--accent)_20%,var(--bg-secondary))] text-[var(--accent)]'
  };
</script>

<span class="inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium {colors[variant]}">
  {label}
</span>
