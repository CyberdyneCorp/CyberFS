<script lang="ts">
  import { api } from "$lib/app";
  import LoadState from "$lib/components/LoadState.svelte";
  import PageHeader from "$lib/components/PageHeader.svelte";
  import { formatDateTime, formatDuration } from "$lib/format";
  import { createHealthVM, type Overall } from "./health.vm.svelte";

  const vm = createHealthVM(api());

  const PURGEABLE = [
    { dataset: "listing", label: "Folder listings" },
    { dataset: "perm", label: "Permission decisions" },
    { dataset: "meta", label: "Node metadata" },
  ];

  const SUMMARY: Record<Overall, { pill: string; text: string }> = {
    healthy: { pill: "ok", text: "All dependencies are responding." },
    degraded: {
      pill: "warn",
      text: "An optional dependency is down. CyberFS is slower but still correct.",
    },
    unhealthy: { pill: "danger", text: "A required dependency is down. Requests are failing." },
    unknown: { pill: "", text: "No dependency status reported." },
  };

  $effect(() => {
    void vm.load();
  });
</script>

<svelte:head><title>Health · CyberFS Admin</title></svelte:head>

<PageHeader title="Health" description="Dependencies, background jobs, and the cache.">
  {#snippet actions()}
    <button type="button" onclick={() => vm.load()} disabled={vm.state.loading}>
      Refresh
    </button>
  {/snippet}
</PageHeader>

<LoadState
  loading={vm.state.loading && !vm.state.data}
  error={vm.state.error}
  onRetry={vm.load}
>
  {#if vm.state.data}
    <div class="card">
      <h2>
        Status <span class="pill {SUMMARY[vm.overall].pill}">{vm.overall}</span>
      </h2>
      <p class="muted">{SUMMARY[vm.overall].text}</p>
      {#if !vm.state.data.totals_reconcile}
        <p class="notice error" role="alert">
          Stored totals do not reconcile with per-user usage. Run the reconciliation job.
        </p>
      {/if}
    </div>

    <section class="section">
      <h2>Dependencies</h2>
      <div class="card table-wrap">
        <table>
          <caption>Required dependencies must be up; optional ones only degrade</caption>
          <thead>
            <tr>
              <th scope="col">Component</th>
              <th scope="col">Status</th>
              <th scope="col">Criticality</th>
              <th scope="col" class="num">Latency</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {#each vm.state.data.components as component (component.name)}
              <tr>
                <th scope="row">{component.name}</th>
                <td>
                  <span
                    class="pill {component.status === 'up'
                      ? 'ok'
                      : component.criticality === 'required'
                        ? 'danger'
                        : 'warn'}"
                  >
                    {component.status}
                  </span>
                </td>
                <td>{component.criticality}</td>
                <td class="num">
                  {component.latency_ms === null ? "—" : `${component.latency_ms} ms`}
                </td>
                <td>{component.detail ?? "—"}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section class="section">
      <h2>Background jobs</h2>
      {#if vm.staleJobs.length > 0}
        <p class="notice" role="status">
          {vm.staleJobs.map((job) => job.name).join(", ")} has never run. On a new deployment that
          usually means the scheduler is not wired up.
        </p>
      {/if}
      <div class="card table-wrap">
        <table>
          <caption>Last run per scheduled job</caption>
          <thead>
            <tr>
              <th scope="col">Job</th>
              <th scope="col">Last run</th>
              <th scope="col">Outcome</th>
              <th scope="col" class="num">Duration</th>
              <th scope="col">Detail</th>
            </tr>
          </thead>
          <tbody>
            {#each vm.state.data.jobs as job (job.name)}
              <tr>
                <th scope="row">{job.name}</th>
                <td>{formatDateTime(job.last_run_at)}</td>
                <td>
                  {#if !job.has_run}
                    <span class="pill warn">never run</span>
                  {:else}
                    <span class="pill {job.outcome === 'success' ? 'ok' : 'danger'}">
                      {job.outcome ?? "unknown"}
                    </span>
                  {/if}
                </td>
                <td class="num">{formatDuration(job.duration_seconds)}</td>
                <td>{job.detail ?? "—"}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <section class="section">
      <h2>Cache</h2>
      <div class="card">
        <p>
          Redis is
          <span class="pill {vm.cacheAvailable ? 'ok' : 'warn'}">
            {vm.cacheAvailable ? "available" : "unavailable"}
          </span>
        </p>
        <p class="muted">
          The cache only accelerates reads. When it is unavailable CyberFS answers from Postgres
          and MinIO instead, so purging is always safe.
        </p>
        {#if vm.state.notice}
          <p class="notice" role="status">{vm.state.notice}</p>
        {/if}
        <div class="toolbar" style="margin: 0" role="group" aria-label="Purge cache">
          {#each PURGEABLE as target (target.dataset)}
            <button
              type="button"
              disabled={vm.state.purging !== null || !vm.cacheAvailable}
              onclick={() => vm.purge(target.dataset)}
            >
              {vm.state.purging === target.dataset ? "Purging…" : `Purge ${target.label}`}
            </button>
          {/each}
        </div>
      </div>
    </section>
  {/if}
</LoadState>
