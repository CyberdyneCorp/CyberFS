<script lang="ts">
  import { page } from "$app/state";
  import { api } from "$lib/app";
  import Bar from "$lib/components/Bar.svelte";
  import LoadState from "$lib/components/LoadState.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import Stat from "$lib/components/Stat.svelte";
  import { formatBytes, formatDateTime, formatPercent, quotaSeverity } from "$lib/format";
  import { createUserDetailVM } from "./user-detail.vm.svelte";

  const vm = createUserDetailVM(api(), page.params.userId ?? "");

  $effect(() => {
    void vm.load();
  });

  const user = $derived(vm.state.user);
</script>

<svelte:head><title>{user?.subject ?? "User"} · CyberFS Admin</title></svelte:head>

<PageHeader
  title={user?.subject ?? "User"}
  description="Storage breakdown and quota. File names and contents are not shown: they are
    encrypted under keys this dashboard does not hold."
/>

<p><a href="/users">← All users</a></p>

<LoadState loading={vm.state.loading} error={vm.state.error} onRetry={vm.load}>
  {#if user}
    <div class="grid">
      <Stat
        label="Charged"
        value={formatBytes(user.used_bytes)}
        hint="{formatPercent(user.percent_used)} of quota"
      />
      <Stat label="Quota" value={formatBytes(user.quota_bytes)} />
      <Stat
        label="Files"
        value={user.file_count.toLocaleString()}
        hint="{user.folder_count.toLocaleString()} folders"
      />
      <Stat
        label="Encrypted"
        value={formatPercent(user.encrypted_share)}
        hint="{user.encrypted_file_count.toLocaleString()} files, {formatBytes(
          user.encrypted_bytes,
        )}"
      />
      <Stat label="Shares given" value={user.grants_given.toLocaleString()} />
      <Stat label="Shares received" value={user.grants_received.toLocaleString()} />
    </div>

    <section class="section">
      <h2>Where the space goes</h2>
      <div class="card">
        <Bar
          percent={user.percent_used}
          severity={quotaSeverity(user.percent_used, user.over_quota)}
          label="Quota used: {formatPercent(user.percent_used)}"
        />
        <div class="table-wrap" style="margin-top: 1rem">
          <table>
            <caption>Charged bytes by kind</caption>
            <thead>
              <tr>
                <th scope="col">Kind</th>
                <th scope="col" class="num">Bytes</th>
                <th scope="col" class="num">Share</th>
              </tr>
            </thead>
            <tbody>
              {#each vm.breakdown as slice (slice.label)}
                <tr>
                  <th scope="row">{slice.label}</th>
                  <td class="num">{formatBytes(slice.bytes)}</td>
                  <td class="num">{formatPercent(slice.share)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <section class="section">
      <h2>Quota</h2>
      <div class="card">
        <form
          onsubmit={(event) => {
            event.preventDefault();
            void vm.saveQuota();
          }}
        >
          <div class="toolbar">
            <label>
              New quota
              <input
                type="text"
                inputmode="text"
                aria-describedby="quota-help"
                aria-invalid={vm.state.quotaError ? "true" : undefined}
                value={vm.state.quotaInput}
                oninput={(event) => vm.setQuotaInput(event.currentTarget.value)}
              />
            </label>
            <button type="submit" class="primary" disabled={!vm.canSave}>
              {vm.state.saving ? "Saving…" : "Save quota"}
            </button>
          </div>
          <p id="quota-help" class="muted">
            <small>Bytes, or a size such as <code>50 GB</code>.</small>
          </p>
        </form>

        {#if vm.state.quotaError}
          <p class="notice error" role="alert">{vm.state.quotaError}</p>
        {:else if vm.state.saved}
          <p class="notice" role="status">
            Quota set to {formatBytes(user.quota_bytes)}.
            {#if user.over_quota}
              This user is now over quota and cannot upload until they free space.
            {/if}
          </p>
        {/if}
      </div>
    </section>

    <section class="section">
      <h2>Account</h2>
      <div class="card">
        <dl>
          <dt class="muted">Subject</dt>
          <dd>{user.subject}</dd>
          <dt class="muted">Created</dt>
          <dd>{formatDateTime(user.created_at)}</dd>
          <dt class="muted">Last seen</dt>
          <dd>{formatDateTime(user.last_seen_at)}</dd>
        </dl>
      </div>
    </section>
  {/if}
</LoadState>
