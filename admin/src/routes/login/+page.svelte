<script lang="ts">
  import { beginLogin } from "$lib/app";
  import { session } from "$lib/auth/session.svelte";
  import { describeError } from "$lib/errors";

  let starting = $state(false);
  let error = $state<string | null>(null);

  async function signIn(): Promise<void> {
    starting = true;
    error = null;
    try {
      // Returns only if it failed: on success the browser leaves this page.
      await beginLogin(window.location.pathname);
    } catch (err) {
      error = describeError(err);
      starting = false;
    }
  }
</script>

<svelte:head><title>Sign in · CyberFS Admin</title></svelte:head>

<div class="centered">
  <div class="card">
    <h1>CyberFS Administration</h1>
    <p class="muted">
      Sign in with your Cyberdyne account. Administration is limited to accounts CyberdyneAuth
      marks as administrators.
    </p>

    {#if session.error}
      <p class="notice" role="status">{session.error}</p>
    {/if}
    {#if error}
      <p class="notice error" role="alert">{error}</p>
    {/if}

    <button type="button" class="primary" onclick={signIn} disabled={starting}>
      {starting ? "Redirecting…" : "Continue with Cyberdyne"}
    </button>

    <p class="muted" style="margin-bottom: 0">
      <small>
        This dashboard shows storage totals and audit records only. File contents are encrypted
        per user and are not readable here.
      </small>
    </p>
  </div>
</div>
