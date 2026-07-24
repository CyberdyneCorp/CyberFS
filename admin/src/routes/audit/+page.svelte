<script lang="ts">
  import { api } from "$lib/app";
  import LoadState from "$lib/components/LoadState.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import { formatDateTime, humanizeAction } from "$lib/format";
  import { createAuditVM } from "./audit.vm.svelte";

  const vm = createAuditVM(api());

  $effect(() => {
    void vm.load();
  });
</script>

<svelte:head><title>Audit · CyberFS Admin</title></svelte:head>

<PageHeader
  title="Audit"
  description="Every share, revocation, and administrative action. Records are append-only
    and are not editable from this dashboard."
/>

<form
  class="toolbar"
  onsubmit={(event) => {
    event.preventDefault();
    void vm.applyFilters();
  }}
>
  <label>
    Actor
    <input
      type="text"
      value={vm.state.filters.actor}
      oninput={(event) => vm.setFilter("actor", event.currentTarget.value)}
    />
  </label>
  <label>
    Action
    <input
      type="text"
      placeholder="grant.created"
      value={vm.state.filters.action}
      oninput={(event) => vm.setFilter("action", event.currentTarget.value)}
    />
  </label>
  <label>
    Target
    <input
      type="text"
      value={vm.state.filters.target}
      oninput={(event) => vm.setFilter("target", event.currentTarget.value)}
    />
  </label>
  <label>
    From
    <input
      type="datetime-local"
      value={vm.state.filters.since}
      oninput={(event) => vm.setFilter("since", event.currentTarget.value)}
    />
  </label>
  <label>
    To
    <input
      type="datetime-local"
      value={vm.state.filters.until}
      oninput={(event) => vm.setFilter("until", event.currentTarget.value)}
    />
  </label>
  <button type="submit" class="primary">Apply</button>
  {#if vm.isFiltered}
    <button type="button" onclick={() => vm.clearFilters()}>Clear</button>
  {/if}
</form>

<LoadState
  loading={vm.state.loading}
  error={vm.state.error}
  empty={vm.isEmpty}
  emptyMessage={vm.isFiltered ? "No records match these filters." : "No audit records yet."}
  onRetry={vm.load}
>
  <div class="card table-wrap">
    <table>
      <caption>{vm.state.entries.length} records</caption>
      <thead>
        <tr>
          <th scope="col">When</th>
          <th scope="col">Action</th>
          <th scope="col">Actor</th>
          <th scope="col">Target</th>
          <th scope="col">Recipient</th>
          <th scope="col">Source</th>
        </tr>
      </thead>
      <tbody>
        {#each vm.state.entries as entry, index (`${entry.occurred_at}-${index}`)}
          <tr>
            <th scope="row">{formatDateTime(entry.occurred_at)}</th>
            <td>{humanizeAction(entry.action)}</td>
            <td>{entry.actor_subject ?? "system"}</td>
            <td>{entry.target_id ?? "—"}</td>
            <td>{entry.recipient_subject ?? "—"}</td>
            <td>{entry.source_ip ?? "—"}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  {#if vm.hasMore}
    <div class="toolbar" style="margin-top: 1rem">
      <button type="button" disabled={vm.state.loadingMore} onclick={() => vm.loadMore()}>
        {vm.state.loadingMore ? "Loading…" : "Load more"}
      </button>
    </div>
  {/if}
</LoadState>
