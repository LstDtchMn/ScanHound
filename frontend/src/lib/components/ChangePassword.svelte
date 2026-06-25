<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { hasPassword, refreshAuthStatus, setPassword, logout } from '$lib/stores/auth';
  import { addToast } from '$lib/stores/notifications';

  const MIN = 8;

  let current = $state('');
  let next = $state('');
  let confirm = $state('');
  let saving = $state(false);
  let error = $state('');

  onMount(() => { refreshAuthStatus(); });

  async function submit(e: Event) {
    e.preventDefault();
    error = '';
    if (next.length < MIN) { error = `Password must be at least ${MIN} characters.`; return; }
    if (next !== confirm) { error = 'Passwords do not match.'; return; }
    if ($hasPassword && !current) { error = 'Enter your current password.'; return; }
    saving = true;
    try {
      await setPassword(next, $hasPassword ? current : undefined);
      addToast(
        'Password updated',
        $hasPassword ? 'Your password was changed.' : 'Password set — sign-in is now required.'
      );
      current = ''; next = ''; confirm = '';
    } catch {
      error = $hasPassword
        ? 'Could not change password — check your current password.'
        : 'Could not set the password.';
    } finally {
      saving = false;
    }
  }

  function signOut() {
    logout();
    goto('/login');
  }
</script>

<div class="bg-[var(--bg-secondary)] rounded-lg p-5 border border-[var(--border)] space-y-4">
  <div class="flex items-center justify-between">
    <h3 class="text-sm font-semibold text-[var(--text-secondary)] uppercase tracking-wide">
      {$hasPassword ? 'Change password' : 'Set a password'}
    </h3>
    {#if $hasPassword}
      <button onclick={signOut} class="text-xs text-[var(--text-secondary)] hover:text-[var(--text-primary)] underline">Sign out</button>
    {/if}
  </div>
  <p class="text-xs text-[var(--text-secondary)]">
    {#if $hasPassword}
      A password is set — browser and remote clients must sign in. Changing it signs out all existing sessions.
    {:else}
      Set a password to require sign-in for browser / self-hosted access. The desktop app stays auto-authenticated via its sidecar.
    {/if}
  </p>
  <form onsubmit={submit} class="space-y-3">
    {#if $hasPassword}
      <div>
        <label for="cp-current" class="block text-xs font-medium text-[var(--text-secondary)] mb-1">Current password</label>
        <input
          id="cp-current"
          type="password"
          autocomplete="current-password"
          bind:value={current}
          class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
        />
      </div>
    {/if}
    <div>
      <label for="cp-new" class="block text-xs font-medium text-[var(--text-secondary)] mb-1">New password <span class="opacity-60">(min {MIN} characters)</span></label>
      <input
        id="cp-new"
        type="password"
        autocomplete="new-password"
        bind:value={next}
        class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
      />
    </div>
    <div>
      <label for="cp-confirm" class="block text-xs font-medium text-[var(--text-secondary)] mb-1">Confirm new password</label>
      <input
        id="cp-confirm"
        type="password"
        autocomplete="new-password"
        bind:value={confirm}
        class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
      />
    </div>
    {#if error}<p class="text-xs text-[var(--error)]">{error}</p>{/if}
    <button
      type="submit"
      disabled={saving}
      class="px-4 py-2 rounded-lg text-sm font-semibold text-white bg-[var(--accent)] hover:opacity-90 transition disabled:opacity-50"
    >{saving ? 'Saving…' : ($hasPassword ? 'Change password' : 'Set password')}</button>
  </form>
</div>
