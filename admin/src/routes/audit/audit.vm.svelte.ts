// The audit log: filters and cursor pagination.
//
// Pages accumulate rather than replacing, so "load more" reads as a growing
// list. Changing a filter resets that accumulation.

import type { AdminApi } from "$lib/api/endpoints";
import type { AuditEntry, AuditQuery } from "$lib/api/types";
import { describeError } from "$lib/errors";

export interface AuditFilters {
  actor: string;
  action: string;
  target: string;
  since: string;
  until: string;
}

export interface AuditState {
  entries: AuditEntry[];
  filters: AuditFilters;
  cursor: string | null;
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
}

export interface AuditVM {
  readonly state: AuditState;
  readonly hasMore: boolean;
  readonly isEmpty: boolean;
  readonly isFiltered: boolean;
  load(): Promise<void>;
  loadMore(): Promise<void>;
  setFilter<K extends keyof AuditFilters>(key: K, value: string): void;
  applyFilters(): Promise<void>;
  clearFilters(): Promise<void>;
}

const PAGE_SIZE = 100;

const EMPTY: AuditFilters = { actor: "", action: "", target: "", since: "", until: "" };

/** Drops blanks so a cleared field does not narrow the query to nothing. */
export function toQuery(filters: AuditFilters, cursor?: string | null): AuditQuery {
  const query: AuditQuery = { limit: PAGE_SIZE };
  if (filters.actor.trim()) query.actor = filters.actor.trim();
  if (filters.action.trim()) query.action = filters.action.trim();
  if (filters.target.trim()) query.target = filters.target.trim();
  if (filters.since) query.since = new Date(filters.since).toISOString();
  if (filters.until) query.until = new Date(filters.until).toISOString();
  if (cursor) query.cursor = cursor;
  return query;
}

export function createAuditVM(api: AdminApi): AuditVM {
  const state = $state<AuditState>({
    entries: [],
    filters: { ...EMPTY },
    cursor: null,
    loading: false,
    loadingMore: false,
    error: null,
  });

  const hasMore = $derived(state.cursor !== null);
  const isEmpty = $derived(!state.loading && state.entries.length === 0);
  const isFiltered = $derived(
    Object.values(state.filters).some((value) => value.trim() !== ""),
  );

  async function load(): Promise<void> {
    state.loading = true;
    state.error = null;
    try {
      const page = await api.audit(toQuery(state.filters));
      state.entries = page.items;
      state.cursor = page.next_cursor;
    } catch (err) {
      state.error = describeError(err);
      state.entries = [];
      state.cursor = null;
    } finally {
      state.loading = false;
    }
  }

  async function loadMore(): Promise<void> {
    // Guarded so a double click cannot append the same page twice.
    if (!state.cursor || state.loadingMore) return;

    state.loadingMore = true;
    try {
      const page = await api.audit(toQuery(state.filters, state.cursor));
      state.entries = [...state.entries, ...page.items];
      state.cursor = page.next_cursor;
    } catch (err) {
      state.error = describeError(err);
    } finally {
      state.loadingMore = false;
    }
  }

  function setFilter<K extends keyof AuditFilters>(key: K, value: string): void {
    state.filters[key] = value;
  }

  async function applyFilters(): Promise<void> {
    state.cursor = null;
    await load();
  }

  async function clearFilters(): Promise<void> {
    state.filters = { ...EMPTY };
    await applyFilters();
  }

  return {
    get state() {
      return state;
    },
    get hasMore() {
      return hasMore;
    },
    get isEmpty() {
      return isEmpty;
    },
    get isFiltered() {
      return isFiltered;
    },
    load,
    loadMore,
    setFilter,
    applyFilters,
    clearFilters,
  };
}
