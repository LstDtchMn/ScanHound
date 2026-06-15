<script lang="ts">
  import { connection } from '$lib/stores/connection';
  import { fly } from 'svelte/transition';

  const connectionState = connection.state;

  let hasConnected = $state(false);
  let show = $derived(
    hasConnected && ($connectionState === 'disconnected' || $connectionState === 'reconnecting' || $connectionState === 'failed')
  );

  $effect(() => {
    if ($connectionState === 'connected') hasConnected = true;
  });

  let message = $derived(
    $connectionState === 'failed'
      ? 'Backend failed to start. Please restart the app.'
      : $connectionState === 'reconnecting'
        ? 'Backend restarting...'
        : 'Connection lost — retrying...'
  );

  let color = $derived(
    $connectionState === 'failed' ? 'var(--error)' : 'var(--warning)'
  );
</script>

{#if show}
  <div
    transition:fly={{ y: -32, duration: 200 }}
    class="flex items-center justify-center gap-2 px-4 py-1.5 border-b text-xs"
    style="background: color-mix(in srgb, {color} 15%, var(--bg-primary)); border-color: color-mix(in srgb, {color} 30%, transparent); color: {color};"
  >
    {#if $connectionState !== 'failed'}
      <span class="w-1.5 h-1.5 rounded-full animate-pulse" style="background: {color};"></span>
    {:else}
      <svg class="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="2">
        <path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
      </svg>
    {/if}
    {message}
  </div>
{/if}
