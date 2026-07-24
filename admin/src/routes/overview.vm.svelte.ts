// Overview view model: deployment-wide totals, growth, top consumers.
//
// All state, loading, and error handling live here so the component is purely
// presentational and this can be exercised without a DOM.

import type { AdminApi } from "$lib/api/endpoints";
import type { TenantSummary } from "$lib/api/types";
import { describeError } from "$lib/errors";

export type GrowthWindow = 7 | 30 | 90;

export interface OverviewState {
  data: TenantSummary | null;
  window: GrowthWindow;
  loading: boolean;
  error: string | null;
}

export interface OverviewVM {
  readonly state: OverviewState;
  readonly maxGrowthBytes: number;
  readonly hasData: boolean;
  load(): Promise<void>;
  setWindow(days: GrowthWindow): Promise<void>;
}

export function createOverviewVM(api: AdminApi): OverviewVM {
  const state = $state<OverviewState>({
    data: null,
    window: 30,
    loading: false,
    error: null,
  });

  // Scales the growth bars. Guarded so an all-zero window does not divide by
  // zero and render nothing at all.
  const maxGrowthBytes = $derived(
    Math.max(1, ...(state.data?.growth ?? []).map((point) => point.bytes_added)),
  );
  const hasData = $derived(state.data !== null);

  async function load(): Promise<void> {
    state.loading = true;
    state.error = null;
    try {
      state.data = await api.overview(state.window);
    } catch (err) {
      state.error = describeError(err);
      // Keep whatever was on screen: a failed refresh should not blank the
      // page the operator was reading.
    } finally {
      state.loading = false;
    }
  }

  async function setWindow(days: GrowthWindow): Promise<void> {
    if (days === state.window) return;
    state.window = days;
    await load();
  }

  return {
    get state() {
      return state;
    },
    get maxGrowthBytes() {
      return maxGrowthBytes;
    },
    get hasData() {
      return hasData;
    },
    load,
    setWindow,
  };
}
