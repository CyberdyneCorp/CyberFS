<script lang="ts">
  import { api } from "$lib/app";
  import Bar from "$lib/components/Bar.svelte";
  import LoadState from "$lib/components/LoadState.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import Stat from "$lib/components/Stat.svelte";
  import { formatBytes, formatPercent } from "$lib/format";
  import { createOverviewVM, type GrowthWindow } from "./overview.vm.svelte";

  const vm = createOverviewVM(api());
  const WINDOWS: GrowthWindow[] = [7, 30, 90];

  $effect(() => {
    void vm.load();
  });

  const data = $derived(vm.state.data);
</script>

<svelte:head><title>Overview · CyberFS Admin</title></svelte:head>

<PageHeader
  title="Overview"
  description="Storage across the whole deployment. Figures are aggregates only — file
    contents are encrypted per user and cannot be read from here."
/>

<LoadState loading={vm.state.loading && !vm.hasData} error={vm.state.error} onRetry={vm.load}>
  {#if data}
    <div class="grid">
      <Stat
        label="Stored"
        value={formatBytes(data.total_bytes)}
        hint="{formatBytes(data.live_bytes)} live"
      />
      <Stat label="Trash" value={formatBytes(data.trashed_bytes)} hint="reclaimable on purge" />
      <Stat label="Versions" value={formatBytes(data.version_bytes)} hint="prior revisions" />
      <Stat
        label="Files"
        value={data.file_count.toLocaleString()}
        hint="{data.folder_count.toLocaleString()} folders"
      />
      <Stat
        label="Users"
        value={data.user_count.toLocaleString()}
        hint="{data.active_user_count.toLocaleString()} active"
      />
      <Stat
        label="Shares"
        value={data.grant_count.toLocaleString()}
        hint="{data.public_link_count.toLocaleString()} public links"
      />
    </div>

    <section class="section">
      <h2>Encryption adoption</h2>
      <div class="card">
        <p>
          {data.encrypted_file_count.toLocaleString()} of
          {data.file_count.toLocaleString()} files are encrypted, covering
          {formatBytes(data.encrypted_bytes)}.
        </p>
        <Bar
          percent={data.encrypted_share}
          label="Share of files encrypted: {formatPercent(data.encrypted_share)}"
        />
      </div>
    </section>

    <section class="section">
      <div class="page-header">
        <h2 id="growth-heading">Growth</h2>
        <div class="toolbar" style="margin: 0" role="group" aria-label="Growth window">
          {#each WINDOWS as days (days)}
            <button
              type="button"
              class={vm.state.window === days ? "primary" : ""}
              aria-pressed={vm.state.window === days}
              onclick={() => vm.setWindow(days)}
            >
              {days} days
            </button>
          {/each}
        </div>
      </div>

      <div class="card">
        {#if data.growth.length === 0}
          <p class="empty">No uploads in this window.</p>
        {:else}
          <!-- The chart is decoration; the table below it carries the data. -->
          <div class="chart" aria-hidden="true">
            {#each data.growth as point (point.day)}
              <div style="height: {(100 * point.bytes_added) / vm.maxGrowthBytes}%"></div>
            {/each}
          </div>
          <div class="table-wrap">
            <table>
              <caption>Bytes and files added per day</caption>
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  <th scope="col" class="num">Files added</th>
                  <th scope="col" class="num">Bytes added</th>
                </tr>
              </thead>
              <tbody>
                {#each data.growth as point (point.day)}
                  <tr>
                    <th scope="row">{point.day}</th>
                    <td class="num">{point.files_added.toLocaleString()}</td>
                    <td class="num">{formatBytes(point.bytes_added)}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </div>
    </section>

    <section class="section">
      <h2>Top consumers</h2>
      <div class="card table-wrap">
        <table>
          <caption>Users ranked by charged storage</caption>
          <thead>
            <tr>
              <th scope="col">User</th>
              <th scope="col" class="num">Used</th>
              <th scope="col" class="num">Quota</th>
              <th scope="col" class="num">Files</th>
            </tr>
          </thead>
          <tbody>
            {#each data.top_consumers as user (user.user_id)}
              <tr>
                <th scope="row"><a href="/users/{user.user_id}">{user.subject}</a></th>
                <td class="num">{formatBytes(user.used_bytes)}</td>
                <td class="num">{formatBytes(user.quota_bytes)}</td>
                <td class="num">{user.file_count.toLocaleString()}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section class="section">
      <h2>Content types</h2>
      <div class="card table-wrap">
        <table>
          <caption>Stored bytes by content type</caption>
          <thead>
            <tr>
              <th scope="col">Type</th>
              <th scope="col" class="num">Files</th>
              <th scope="col" class="num">Bytes</th>
            </tr>
          </thead>
          <tbody>
            {#each data.content_types as slice (slice.content_type)}
              <tr>
                <th scope="row">{slice.content_type}</th>
                <td class="num">{slice.file_count.toLocaleString()}</td>
                <td class="num">{formatBytes(slice.bytes)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>
  {/if}
</LoadState>
