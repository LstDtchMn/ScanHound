<script lang="ts">
  import BottomSheet from './BottomSheet.svelte';
  import { selectedKeys } from '$lib/stores/results';
  import { downloadHost } from '$lib/stores/downloads';
  import { buildResultActions } from '$lib/resultActions';
  import type { ScanResult } from '$lib/api/types';

  interface Props {
    item: ScanResult | null;
    onclose: () => void;
  }
  let { item, onclose }: Props = $props();

  let selected = $derived(!!item && $selectedKeys.has(item.url));
  let actions = $derived(item ? buildResultActions(item, $downloadHost, selected) : []);

  function run(fn: () => void) {
    fn();
    onclose();
  }

  const rowClass = 'w-full text-left px-3 py-3 rounded-lg text-sm text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] active:bg-[var(--bg-tertiary)] transition-colors flex items-center gap-3';
</script>

<BottomSheet open={!!item} title={item?.title} onclose={onclose}>
  {#if item}
    <div class="space-y-1">
      {#each actions as action}
        <button class={rowClass} onclick={() => run(action.run)}>
          <span class="w-5 text-center {action.key === 'openImdb' || action.key === 'openInPlex' ? 'font-bold text-[10px]' : ''}">{action.icon}</span>{action.label}
        </button>
      {/each}
    </div>
  {/if}
</BottomSheet>
