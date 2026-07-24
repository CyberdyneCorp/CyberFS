<script lang="ts">
  // The authentication guard and the chrome around every page.
  //
  // Three outcomes: no token -> login; a token whose owner is not an admin ->
  // the access-denied page; otherwise the dashboard. The admin decision comes
  // from CyberdyneAuth, never from anything this app computes.

  import { goto } from "$app/navigation";
  import { page } from "$app/state";
  import "../app.css";
  import { initSession, loadProfile, onAuthenticationLost, signOut } from "$lib/app";
  import { rememberReturnPath, session } from "$lib/auth/session.svelte";
  import { describeError } from "$lib/errors";
  import type { Snippet } from "svelte";

  const { children }: { children: Snippet } = $props();

  const NAV = [
    { href: "/", label: "Overview" },
    { href: "/users", label: "Users" },
    { href: "/sharing", label: "Sharing" },
    { href: "/audit", label: "Audit" },
    { href: "/health", label: "Health" },
  ];

  /** Pages that run before there is a session, so the guard must skip them. */
  const PUBLIC = ["/login", "/auth/callback", "/forbidden"];

  let ready = $state(false);
  const isPublic = $derived(PUBLIC.includes(page.url.pathname));
  const showChrome = $derived(!isPublic && session.checked && session.isAdmin);

  function toLogin(): void {
    rememberReturnPath(page.url.pathname + page.url.search);
    void goto("/login");
  }

  onAuthenticationLost(toLogin);

  $effect(() => {
    // Runs once: `guard` reads the URL only through the snapshot it takes.
    if (ready) return;
    initSession();
    void guard();
  });

  async function guard(): Promise<void> {
    if (PUBLIC.includes(page.url.pathname)) {
      ready = true;
      return;
    }
    if (!session.accessToken) {
      ready = true;
      toLogin();
      return;
    }
    try {
      const profile = await loadProfile();
      if (profile && !profile.is_admin) {
        await goto("/forbidden");
      }
    } catch (err) {
      // A refresh already failed by the time we get here, so this is not a
      // recoverable expiry -- treat it as no session at all.
      session.error = describeError(err);
      toLogin();
    } finally {
      ready = true;
    }
  }

  function onSignOut(): void {
    signOut();
    void goto("/login");
  }
</script>

<svelte:head>
  <title>CyberFS Admin</title>
</svelte:head>

{#if isPublic}
  {@render children()}
{:else if !ready}
  <div class="centered"><p class="empty">Checking your session…</p></div>
{:else if showChrome}
  <a class="skip-link" href="#main">Skip to main content</a>
  <div class="shell">
    <nav class="sidebar" aria-label="Sections">
      <div class="brand">CyberFS <span>Administration</span></div>
      <div class="nav">
        {#each NAV as item (item.href)}
          <a
            href={item.href}
            aria-current={page.url.pathname === item.href ? "page" : undefined}
          >
            {item.label}
          </a>
        {/each}
      </div>
      <div class="identity">
        <p>Signed in as<br />{session.subject ?? "unknown"}</p>
        <button type="button" onclick={onSignOut}>Sign out</button>
      </div>
    </nav>
    <main class="main" id="main">
      {@render children()}
    </main>
  </div>
{:else}
  <div class="centered"><p class="empty">Redirecting…</p></div>
{/if}
