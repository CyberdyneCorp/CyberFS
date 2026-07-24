<script lang="ts">
  // Where CyberdyneAuth lands the browser after consent, with the tokens in the
  // URL fragment. The fragment is scrubbed from the address bar immediately so
  // it cannot be bookmarked, shared, or replayed by the back button.

  import { goto } from "$app/navigation";
  import { adoptTokens, initSession, loadProfile } from "$lib/app";
  import { parseOAuthError, parseOAuthFragment } from "$lib/auth/fragment";
  import { takeReturnPath } from "$lib/auth/session.svelte";
  import { describeError } from "$lib/errors";

  let error = $state<string | null>(null);

  $effect(() => {
    void complete();
  });

  async function complete(): Promise<void> {
    initSession();

    const denied = parseOAuthError(window.location.hash);
    if (denied) {
      error = denied;
      return;
    }

    const fragment = parseOAuthFragment(window.location.hash);
    if (!fragment) {
      // Someone opened the callback directly; there is nothing to complete.
      await goto("/login");
      return;
    }

    adoptTokens(fragment.accessToken, fragment.refreshToken);
    history.replaceState(null, "", window.location.pathname);

    try {
      const profile = await loadProfile();
      await goto(profile?.is_admin ? takeReturnPath() : "/forbidden");
    } catch (err) {
      error = describeError(err);
    }
  }
</script>

<svelte:head><title>Signing in · CyberFS Admin</title></svelte:head>

<div class="centered">
  <div class="card">
    {#if error}
      <h1>Sign-in failed</h1>
      <p class="notice error" role="alert">{error}</p>
      <a class="button" href="/login">Back to sign in</a>
    {:else}
      <h1>Signing you in…</h1>
      <p class="muted" aria-live="polite">Completing the handshake with CyberdyneAuth.</p>
    {/if}
  </div>
</div>
