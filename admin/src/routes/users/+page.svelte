<script lang="ts">
  import { api } from "$lib/app";
  import Bar from "$lib/components/Bar.svelte";
  import LoadState from "$lib/components/LoadState.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import { formatBytes, formatPercent, formatRelative, quotaSeverity } from "$lib/format";
  import { createUsersVM, INACTIVE_AFTER_DAYS, type SortField } from "./users.vm.svelte";

  const vm = createUsersVM(api());

  const SORTS: { value: SortField; label: string }[] = [
    { value: "used", label: "Storage used" },
    { value: "quota", label: "Quota" },
    { value: "files", label: "File count" },
    { value: "activity", label: "Last seen" },
    { value: "subject", label: "Name" },
  ];

  $effect(() => {
    void vm.load();
  });
</script>

<svelte:head><title>Users · CyberFS Admin</title></svelte:head>

<PageHeader
  title="Users"
  description="Per-user storage and share counts. Names and totals only — no file names,
    previews, or contents are available to administrators."
/>

<div class="toolbar">
  <label>
    Search
    <input
      type="search"
      placeholder="Filter by name"
      value={vm.state.search}
      oninput={(event) => vm.setSearch(event.currentTarget.value)}
    />
  </label>

  <label>
    Sort by
    <select
      value={vm.state.sortBy}
      onchange={(event) => vm.setSort(event.currentTarget.value as SortField)}
    >
      {#each SORTS as option (option.value)}
        <option value={option.value}>{option.label}</option>
      {/each}
    </select>
  </label>

  <label class="inline">
    <input
      type="checkbox"
      checked={vm.state.overQuotaOnly}
      onchange={() => vm.toggleOverQuota()}
    />
    Over quota only ({vm.overQuotaCount})
  </label>

  <label class="inline">
    <input
      type="checkbox"
      checked={vm.state.inactiveOnly}
      onchange={() => vm.toggleInactive()}
    />
    Inactive for {INACTIVE_AFTER_DAYS}+ days
  </label>

  {#if vm.isFiltered}
    <button type="button" onclick={() => vm.clearFilters()}>Clear filters</button>
  {/if}
</div>

<LoadState
  loading={vm.state.loading}
  error={vm.state.error}
  empty={vm.visible.length === 0}
  emptyMessage={vm.isFiltered ? "No users match these filters." : "No users yet."}
  onRetry={vm.load}
>
  <div class="card table-wrap">
    <table>
      <caption>
        {vm.visible.length} of {vm.state.users.length} users
      </caption>
      <thead>
        <tr>
          <th scope="col">User</th>
          <th scope="col" class="num">Used</th>
          <th scope="col" class="num">Quota</th>
          <th scope="col">Usage</th>
          <th scope="col" class="num">Files</th>
          <th scope="col" class="num">Encrypted</th>
          <th scope="col" class="num">Shares</th>
          <th scope="col">Last seen</th>
        </tr>
      </thead>
      <tbody>
        {#each vm.visible as user (user.user_id)}
          <tr>
            <th scope="row">
              <a href="/users/{user.user_id}">{user.subject}</a>
              {#if user.is_admin}<span class="pill">admin</span>{/if}
            </th>
            <td class="num">{formatBytes(user.used_bytes)}</td>
            <td class="num">{formatBytes(user.quota_bytes)}</td>
            <td>
              <Bar
                percent={user.percent_used}
                severity={quotaSeverity(user.percent_used, user.over_quota)}
                label="{user.subject} is using {formatPercent(user.percent_used)} of quota"
              />
              {#if user.over_quota}<span class="pill danger">over</span>{/if}
            </td>
            <td class="num">{user.file_count.toLocaleString()}</td>
            <td class="num">{formatPercent(user.encrypted_share)}</td>
            <td class="num">{(user.grants_given + user.grants_received).toLocaleString()}</td>
            <td>{formatRelative(user.last_seen_at)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
</LoadState>
