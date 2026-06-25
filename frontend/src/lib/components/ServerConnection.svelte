<script lang="ts">
  import { serverUrl, hasRemoteServer, saveServerConfig, clearServerConfig, testServerConnection, currentToken } from '$lib/stores/server';
  import { addToast } from '$lib/stores/notifications';

  interface Props { onsaved?: () => void; }
  let { onsaved }: Props = $props();

  let url = $state($serverUrl || '');
  let token = $state(currentToken());
  let testing = $state(false);
  let result = $state<{ ok: boolean; version?: string; error?: string } | null>(null);

  async function test() {
    testing = true;
    result = null;
    result = await testServerConnection(url, token);
    testing = false;
  }

  async function save() {
    // Verify before persisting so we don't lock the user onto a bad endpoint.
    testing = true;
    const r = await testServerConnection(url, token);
    testing = false;
    result = r;
    if (!r.ok) {
      addToast('Connection failed', r.error || 'Could not reach server', 'error');
      return;
    }
    saveServerConfig(url, token);
    addToast('Connected', `Server saved${r.version ? ` (v${r.version})` : ''}`);
    onsaved?.();
  }

  function reset() {
    clearServerConfig();
    url = '';
    token = '';
    result = null;
    addToast('Reset', 'Reverted to same-origin connection');
    onsaved?.();
  }
</script>

<div class="space-y-3">
  <div>
    <label for="sc-url" class="block text-xs font-medium text-[var(--text-secondary)] mb-1">Server URL</label>
    <input
      id="sc-url"
      type="url"
      inputmode="url"
      autocapitalize="none"
      autocorrect="off"
      bind:value={url}
      placeholder="https://scanhound.turtleland.us"
      class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
    />
  </div>
  <div>
    <label for="sc-token" class="block text-xs font-medium text-[var(--text-secondary)] mb-1">Auth token <span class="opacity-60">(optional — or leave blank and sign in with a password)</span></label>
    <input
      id="sc-token"
      type="password"
      autocapitalize="none"
      autocorrect="off"
      bind:value={token}
      placeholder="Auth token (optional)"
      class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
    />
  </div>

  {#if result}
    <p class="text-xs {result.ok ? 'text-[var(--success)]' : 'text-[var(--error)]'}">
      {result.ok ? `✓ Reachable${result.version ? ` — v${result.version}` : ''}` : `✕ ${result.error}`}
    </p>
  {/if}

  <div class="flex items-center gap-2">
    <button
      onclick={test}
      disabled={testing}
      class="px-3 py-2 rounded-lg text-sm font-medium text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] border border-[var(--border)] transition disabled:opacity-50"
    >{testing ? 'Testing…' : 'Test'}</button>
    <button
      onclick={save}
      disabled={testing}
      class="px-4 py-2 rounded-lg text-sm font-semibold text-white bg-[var(--accent)] hover:opacity-90 transition disabled:opacity-50"
    >Save &amp; connect</button>
    {#if $hasRemoteServer}
      <button
        onclick={reset}
        class="ml-auto px-3 py-2 rounded-lg text-sm text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] transition"
        title="Forget the remote server and use same-origin"
      >Reset</button>
    {/if}
  </div>
</div>
