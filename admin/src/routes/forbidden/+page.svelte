<script lang="ts">
  // Authenticated, but not an administrator. Distinct from the login page on
  // purpose: sending this user back to sign in would loop, because signing in
  // again is exactly what they just did.

  import { goto } from "$app/navigation";
  import { signOut } from "$lib/app";
  import { session } from "$lib/auth/session.svelte";

  function useAnotherAccount(): void {
    signOut();
    void goto("/login");
  }
</script>

<svelte:head><title>Access denied · CyberFS Admin</title></svelte:head>

<div class="centered">
  <div class="card">
    <h1>Access denied</h1>
    <p>
      You are signed in as <strong>{session.subject ?? "an unknown account"}</strong>, which is
      not an administrator of CyberFS.
    </p>
    <p class="muted">
      Administrator access is granted in CyberdyneAuth. Ask an existing administrator to grant
      it, then sign in again.
    </p>
    <button type="button" onclick={useAnotherAccount}>Use another account</button>
  </div>
</div>
