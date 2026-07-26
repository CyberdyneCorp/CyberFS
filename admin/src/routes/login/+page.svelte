<script lang="ts">
  // Two ways in. The OAuth button is first and primary on purpose: that flow
  // never exposes a password to this page, so it is the better one. Password
  // sign-in exists for operators without a usable Cyberdyne identity.
  //
  // The password and any one-time code live in component state for the length of
  // the request and nowhere else -- never in storage, never in the URL.

  import { goto } from "$app/navigation";
  import { beginLogin, beginPasswordLogin, completeMfaLogin, loadProfile } from "$lib/app";
  import { MfaSessionExpiredError } from "$lib/auth/auth-client";
  import { session, takeReturnPath } from "$lib/auth/session.svelte";
  import { describeError } from "$lib/errors";

  let starting = $state(false);
  let error = $state<string | null>(null);

  /** `code` is only reachable once CyberdyneAuth has asked for a second factor. */
  let step = $state<"credentials" | "code">("credentials");
  let email = $state("");
  let password = $state("");
  let code = $state("");
  let mfaToken = $state<string | null>(null);
  let busy = $state(false);

  const canSubmit = $derived(email.trim().length > 0 && password.length > 0 && !busy);

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

  async function signInWithPassword(): Promise<void> {
    busy = true;
    error = null;
    try {
      const result = await beginPasswordLogin(email, password, window.location.pathname);
      if (result.kind === "code-required") {
        mfaToken = result.mfaToken;
        step = "code";
        // Drop the password as soon as it is no longer needed.
        password = "";
        return;
      }
      await admit();
    } catch (err) {
      error = describeError(err);
    } finally {
      busy = false;
    }
  }

  async function submitCode(): Promise<void> {
    if (!mfaToken) return;
    busy = true;
    error = null;
    try {
      await completeMfaLogin(mfaToken, code.trim());
      await admit();
    } catch (err) {
      error = describeError(err);
      if (err instanceof MfaSessionExpiredError) {
        // The challenge is dead; no code will ever work. Start over.
        restart();
      }
    } finally {
      busy = false;
    }
  }

  /** Shared tail of both paths, and the same one `/auth/callback` uses. */
  async function admit(): Promise<void> {
    const profile = await loadProfile();
    await goto(profile?.is_admin ? takeReturnPath() : "/forbidden");
  }

  function restart(): void {
    step = "credentials";
    mfaToken = null;
    code = "";
    password = "";
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

    {#if step === "credentials"}
      <button type="button" class="primary" onclick={signIn} disabled={starting || busy}>
        {starting ? "Redirecting…" : "Continue with Cyberdyne"}
      </button>

      <p class="divider"><span>or sign in with a password</span></p>

      <form
        class="credentials"
        onsubmit={(event) => {
          event.preventDefault();
          void signInWithPassword();
        }}
      >
        <label>
          Email
          <input
            type="email"
            name="email"
            autocomplete="username"
            bind:value={email}
            required
            disabled={busy}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            name="password"
            autocomplete="current-password"
            bind:value={password}
            required
            disabled={busy}
          />
        </label>
        <button type="submit" disabled={!canSubmit}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    {:else}
      <form
        class="credentials"
        onsubmit={(event) => {
          event.preventDefault();
          void submitCode();
        }}
      >
        <p class="muted" aria-live="polite">Enter the code from your authenticator app.</p>
        <label>
          Authentication code
          <input
            type="text"
            name="code"
            inputmode="numeric"
            autocomplete="one-time-code"
            bind:value={code}
            required
            disabled={busy}
          />
        </label>
        <button type="submit" class="primary" disabled={busy || code.trim().length === 0}>
          {busy ? "Verifying…" : "Verify"}
        </button>
        <button type="button" onclick={restart} disabled={busy}>Start over</button>
      </form>
    {/if}

    <p class="muted" style="margin-bottom: 0">
      <small>
        This dashboard shows storage totals and audit records only. File contents are encrypted
        per user and are not readable here.
      </small>
    </p>
  </div>
</div>

<style>
  .divider {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    color: var(--muted);
    font-size: 0.8125rem;
    margin: 1rem 0 0.75rem;
  }
  .divider::before,
  .divider::after {
    content: "";
    flex: 1;
    height: 1px;
    background: var(--border);
  }
  .credentials {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    margin-bottom: 1rem;
  }
</style>
