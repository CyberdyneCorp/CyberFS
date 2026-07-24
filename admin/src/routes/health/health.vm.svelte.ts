// Dependency health, background jobs, and the cache purge control.
//
// The distinction this view exists to make clear: a failing *optional*
// dependency means degraded, not down. Redis being unreachable makes CyberFS
// slower, never wrong, and the page must say so rather than showing red.

import type { AdminApi } from "$lib/api/endpoints";
import type { HealthComponent, JobSummary, OperationsSummary } from "$lib/api/types";
import { describeError } from "$lib/errors";

export type Overall = "healthy" | "degraded" | "unhealthy" | "unknown";

export interface HealthState {
  data: OperationsSummary | null;
  loading: boolean;
  error: string | null;
  purging: string | null;
  notice: string | null;
}

export interface HealthVM {
  readonly state: HealthState;
  readonly overall: Overall;
  readonly failing: HealthComponent[];
  readonly degraded: HealthComponent[];
  readonly staleJobs: JobSummary[];
  readonly cacheAvailable: boolean;
  load(): Promise<void>;
  purge(dataset: string): Promise<void>;
}

/**
 * Folds component health into one verdict.
 *
 * A required component down is unhealthy; an optional one down is degraded.
 * This mirrors the readiness rule on the server so the two never disagree.
 */
export function overallStatus(components: HealthComponent[]): Overall {
  if (components.length === 0) return "unknown";
  if (components.some((c) => c.status === "down" && c.criticality === "required")) {
    return "unhealthy";
  }
  if (components.some((c) => c.status === "down")) return "degraded";
  return "healthy";
}

export function createHealthVM(api: AdminApi): HealthVM {
  const state = $state<HealthState>({
    data: null,
    loading: false,
    error: null,
    purging: null,
    notice: null,
  });

  const components = $derived(state.data?.components ?? []);
  const overall = $derived(overallStatus(components));
  const failing = $derived(
    components.filter((c) => c.status === "down" && c.criticality === "required"),
  );
  const degraded = $derived(
    components.filter((c) => c.status === "down" && c.criticality === "optional"),
  );
  // A job that has never run is worth surfacing: on a fresh deployment it means
  // the scheduler is not wired, which is easy to miss.
  const staleJobs = $derived((state.data?.jobs ?? []).filter((job) => !job.has_run));
  const cacheAvailable = $derived(Boolean(state.data?.cache?.available));

  async function load(): Promise<void> {
    state.loading = true;
    state.error = null;
    try {
      state.data = await api.operations();
    } catch (err) {
      state.error = describeError(err);
    } finally {
      state.loading = false;
    }
  }

  async function purge(dataset: string): Promise<void> {
    state.purging = dataset;
    state.notice = null;
    try {
      const result = await api.purgeCache(dataset);
      state.notice = `Purged ${result.keys_removed} ${dataset} entries.`;
      // Refresh so the key count reflects the purge.
      await load();
    } catch (err) {
      state.error = describeError(err);
    } finally {
      state.purging = null;
    }
  }

  return {
    get state() {
      return state;
    },
    get overall() {
      return overall;
    },
    get failing() {
      return failing;
    },
    get degraded() {
      return degraded;
    },
    get staleJobs() {
      return staleJobs;
    },
    get cacheAvailable() {
      return cacheAvailable;
    },
    load,
    purge,
  };
}
