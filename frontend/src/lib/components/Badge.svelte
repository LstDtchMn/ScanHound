<script lang="ts" module>
  export type BadgeVariant = 'default' | 'success' | 'warning' | 'error' | 'accent' | 'info' | 'orange';
</script>

<script lang="ts">
  interface Props {
    label: string;
    variant?: BadgeVariant;
    size?: 'sm' | 'xs' | 'lg';
  }
  let { label, variant = 'default', size = 'sm' }: Props = $props();

  const colors: Record<string, string> = {
    default: 'bg-[var(--bg-tertiary)] text-[var(--text-secondary)]',
    success: 'bg-[color-mix(in_srgb,var(--success)_20%,var(--bg-secondary))] text-[var(--success)]',
    warning: 'bg-[color-mix(in_srgb,var(--warning)_20%,var(--bg-secondary))] text-[var(--warning)]',
    error: 'bg-[color-mix(in_srgb,var(--error)_20%,var(--bg-secondary))] text-[var(--error)]',
    accent: 'bg-[color-mix(in_srgb,var(--accent)_20%,var(--bg-secondary))] text-[var(--accent)]',
    info: 'bg-blue-500/20 text-blue-400',
    orange: 'bg-[color-mix(in_srgb,#f97316_20%,var(--bg-secondary))] text-[#f97316]'
  };

  const sizes: Record<string, string> = {
    sm: 'px-1.5 py-0.5 text-[10px]',
    xs: 'px-1 py-px text-[9px]',
    lg: 'px-2 py-1 text-sm'
  };
</script>

<span class="inline-flex items-center rounded font-medium {colors[variant]} {sizes[size]}">
  {label}
</span>
