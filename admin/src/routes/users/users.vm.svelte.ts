// Users view model: sorting, filtering, and the client-side search that the
// API does not provide.

import type { AdminApi } from "$lib/api/endpoints";
import type { UserStorage } from "$lib/api/types";
import { describeError } from "$lib/errors";

export type SortField = "used" | "quota" | "files" | "activity" | "subject";

/** Longer than this without a request counts as inactive. */
export const INACTIVE_AFTER_DAYS = 30;

export interface UsersState {
  users: UserStorage[];
  sortBy: SortField;
  overQuotaOnly: boolean;
  inactiveOnly: boolean;
  search: string;
  loading: boolean;
  error: string | null;
}

export interface UsersVM {
  readonly state: UsersState;
  readonly visible: UserStorage[];
  readonly overQuotaCount: number;
  readonly isFiltered: boolean;
  load(): Promise<void>;
  setSort(field: SortField): Promise<void>;
  toggleOverQuota(): Promise<void>;
  toggleInactive(): void;
  setSearch(term: string): void;
  clearFilters(): Promise<void>;
}

export function isInactive(user: UserStorage, now: Date = new Date()): boolean {
  if (!user.last_seen_at) return true;
  const seen = new Date(user.last_seen_at).getTime();
  if (Number.isNaN(seen)) return true;
  return now.getTime() - seen > INACTIVE_AFTER_DAYS * 86_400_000;
}

const systemClock = (): Date => new Date();

export function createUsersVM(api: AdminApi, clock: () => Date = systemClock): UsersVM {
  const state = $state<UsersState>({
    users: [],
    sortBy: "used",
    overQuotaOnly: false,
    inactiveOnly: false,
    search: "",
    loading: false,
    error: null,
  });

  // Sorting and the over-quota filter are the API's job -- it can do them
  // across the whole set. Search and the inactive filter are applied here,
  // over what was returned.
  const visible = $derived.by(() => {
    const term = state.search.trim().toLowerCase();
    const now = clock();
    return state.users.filter((user) => {
      if (state.inactiveOnly && !isInactive(user, now)) return false;
      if (term && !user.subject.toLowerCase().includes(term)) return false;
      return true;
    });
  });

  const overQuotaCount = $derived(state.users.filter((user) => user.over_quota).length);
  const isFiltered = $derived(
    state.overQuotaOnly || state.inactiveOnly || state.search.trim() !== "",
  );

  async function load(): Promise<void> {
    state.loading = true;
    state.error = null;
    try {
      const page = await api.users({
        sort_by: state.sortBy,
        over_quota: state.overQuotaOnly,
        limit: 500,
      });
      state.users = page.items;
    } catch (err) {
      state.error = describeError(err);
    } finally {
      state.loading = false;
    }
  }

  async function setSort(field: SortField): Promise<void> {
    if (field === state.sortBy) return;
    state.sortBy = field;
    await load();
  }

  async function toggleOverQuota(): Promise<void> {
    state.overQuotaOnly = !state.overQuotaOnly;
    await load();
  }

  function toggleInactive(): void {
    // Purely local, so no round trip.
    state.inactiveOnly = !state.inactiveOnly;
  }

  function setSearch(term: string): void {
    state.search = term;
  }

  async function clearFilters(): Promise<void> {
    const needsReload = state.overQuotaOnly;
    state.overQuotaOnly = false;
    state.inactiveOnly = false;
    state.search = "";
    if (needsReload) await load();
  }

  return {
    get state() {
      return state;
    },
    get visible() {
      return visible;
    },
    get overQuotaCount() {
      return overQuotaCount;
    },
    get isFiltered() {
      return isFiltered;
    },
    load,
    setSort,
    toggleOverQuota,
    toggleInactive,
    setSearch,
    clearFilters,
  };
}
