<script lang="ts">
  import { page } from "$app/state";
</script>

<svelte:head><title>Error · CyberFS Admin</title></svelte:head>

<div class="centered">
  <div class="card">
    <h1>{page.status}</h1>
    <p>{page.error?.message ?? "Something went wrong."}</p>
    <a class="button" href="/">Back to the overview</a>
  </div>
</div>
