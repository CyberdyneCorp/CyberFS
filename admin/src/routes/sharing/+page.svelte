<script lang="ts">
  import { api } from "$lib/app";
  import LoadState from "$lib/components/LoadState.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import Stat from "$lib/components/Stat.svelte";
  import { formatDateTime, formatRelative } from "$lib/format";
  import { createSharingVM, isExpiringSoon } from "./sharing.vm.svelte";

  const vm = createSharingVM(api());

  $effect(() => {
    void vm.load();
  });

  const pending = $derived(vm.state.links.find((link) => link.id === vm.state.pendingRevoke));
</script>

<svelte:head><title>Sharing · CyberFS Admin</title></svelte:head>

<PageHeader
  title="Sharing"
  description="Public links currently in circulation. Revoking one stops it working
    immediately for everyone holding the URL."
/>

<div class="grid">
  <Stat label="Active links" value={vm.state.links.length.toLocaleString()} />
  <Stat
    label="Without a passphrase"
    value={vm.unprotectedCount.toLocaleString()}
    hint="readable by anyone with the URL"
  />
  <Stat label="Expiring within 7 days" value={vm.expiringSoon.length.toLocaleString()} />
</div>

{#if vm.state.notice}
  <p class="notice" role="status" style="margin-top: 1rem">{vm.state.notice}</p>
{/if}

{#if pending}
  <div class="notice error" role="alertdialog" aria-labelledby="revoke-title">
    <h2 id="revoke-title">Revoke this link?</h2>
    <p>
      The link created by {pending.created_by} on {formatDateTime(pending.created_at)} stops working
      for everyone immediately. This cannot be undone.
    </p>
    <div class="toolbar" style="margin: 0">
      <button type="button" class="danger" onclick={() => vm.confirmRevoke()}>
        Revoke link
      </button>
      <button type="button" onclick={() => vm.cancelRevoke()}>Cancel</button>
    </div>
  </div>
{/if}

<section class="section">
  <LoadState
    loading={vm.state.loading}
    error={vm.state.error}
    empty={vm.state.links.length === 0}
    emptyMessage="No public links are active."
    onRetry={vm.load}
  >
    <div class="card table-wrap">
      <table>
        <caption>Active public links</caption>
        <thead>
          <tr>
            <th scope="col">Created by</th>
            <th scope="col">Created</th>
            <th scope="col">Expires</th>
            <th scope="col">Protection</th>
            <th scope="col" class="num">Accesses</th>
            <th scope="col">Last accessed</th>
            <th scope="col"><span class="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {#each vm.state.links as link (link.id)}
            <tr>
              <th scope="row">{link.created_by}</th>
              <td>{formatDateTime(link.created_at)}</td>
              <td>
                {formatDateTime(link.expires_at)}
                {#if isExpiringSoon(link)}<span class="pill warn">soon</span>{/if}
              </td>
              <td>
                {#if link.passphrase_protected}
                  <span class="pill ok">passphrase</span>
                {:else}
                  <span class="pill warn">none</span>
                {/if}
              </td>
              <td class="num">{link.access_count.toLocaleString()}</td>
              <td>{formatRelative(link.last_accessed_at)}</td>
              <td>
                <button
                  type="button"
                  class="danger"
                  disabled={vm.state.revoking === link.id}
                  onclick={() => vm.askRevoke(link.id)}
                >
                  {vm.state.revoking === link.id ? "Revoking…" : "Revoke"}
                  <span class="sr-only">link created by {link.created_by}</span>
                </button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  </LoadState>
</section>
