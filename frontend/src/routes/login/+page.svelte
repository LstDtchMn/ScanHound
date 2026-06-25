<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { login, refreshAuthStatus } from '$lib/stores/auth';

  let password = $state('');
  let submitting = $state(false);
  let error = $state('');
  let inputEl = $state<HTMLInputElement>();

  onMount(async () => {
    inputEl?.focus();
    // If the server doesn't actually require auth, skip the login screen.
    const status = await refreshAuthStatus();
    if (status && !status.auth_required) goto('/');
  });

  async function submit(e: Event) {
    e.preventDefault();
    if (!password || submitting) return;
    submitting = true;
    error = '';
    try {
      await login(password);
      // Full reload so the app re-initialises cleanly with the new token
      // (WebSocket + first data fetch) rather than from partial in-place state.
      if (typeof window !== 'undefined') window.location.href = '/';
    } catch {
      error = 'Incorrect password. Please try again.';
      password = '';
      submitting = false;
    }
  }
</script>

<div class="min-h-screen flex items-center justify-center bg-[var(--bg-primary)] text-[var(--text-primary)] p-4">
  <form
    onsubmit={submit}
    class="w-full max-w-sm bg-[var(--bg-secondary)] border border-[var(--border)] rounded-2xl shadow-2xl p-6 space-y-4"
  >
    <div class="text-center space-y-1">
      <h1 class="text-xl font-bold text-[var(--accent)]">ScanHound</h1>
      <p class="text-xs text-[var(--text-secondary)]">Enter your password to continue</p>
    </div>
    <div>
      <label for="login-password" class="sr-only">Password</label>
      <input
        id="login-password"
        type="password"
        autocomplete="current-password"
        bind:this={inputEl}
        bind:value={password}
        placeholder="Password"
        disabled={submitting}
        class="w-full bg-[var(--bg-tertiary)] text-[var(--text-primary)] px-3 py-2.5 rounded-lg border border-[var(--border)] text-sm focus:outline-none focus:border-[var(--accent)]"
      />
    </div>
    {#if error}
      <p class="text-xs text-[var(--error)]">{error}</p>
    {/if}
    <button
      type="submit"
      disabled={submitting || !password}
      class="w-full px-4 py-2.5 rounded-lg text-sm font-semibold text-white bg-[var(--accent)] hover:opacity-90 transition disabled:opacity-50"
    >{submitting ? 'Signing in…' : 'Sign in'}</button>
  </form>
</div>
