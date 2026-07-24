// One user's storage, plus quota editing.

import type { AdminApi } from "$lib/api/endpoints";
import type { UserStorage } from "$lib/api/types";
import { describeError } from "$lib/errors";

export interface UserDetailState {
  user: UserStorage | null;
  loading: boolean;
  error: string | null;
  /** Free text, because the field accepts "50 GB" as readily as a number. */
  quotaInput: string;
  saving: boolean;
  saved: boolean;
  quotaError: string | null;
}

export interface UsageSlice {
  label: string;
  bytes: number;
  /** Share of the user's charged total, for a stacked bar. */
  share: number;
}

export interface UserDetailVM {
  readonly state: UserDetailState;
  readonly breakdown: UsageSlice[];
  readonly canSave: boolean;
  load(): Promise<void>;
  setQuotaInput(value: string): void;
  saveQuota(): Promise<void>;
}

const MULTIPLIERS: Record<string, number> = {
  b: 1,
  kb: 1024,
  mb: 1024 ** 2,
  gb: 1024 ** 3,
  tb: 1024 ** 4,
};

/**
 * Accepts `1073741824`, `1 GB`, or `1gb`.
 *
 * Returns null for anything it cannot read, so the caller can say so rather
 * than silently setting a quota the operator did not intend.
 */
export function parseQuota(input: string): number | null {
  const text = input.trim().toLowerCase().replace(/,/g, "");
  if (!text) return null;

  const match = /^(\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb)?$/.exec(text);
  if (!match) return null;

  const amount = Number.parseFloat(match[1]);
  if (!Number.isFinite(amount) || amount < 0) return null;

  return Math.round(amount * MULTIPLIERS[match[2] ?? "b"]);
}

export function createUserDetailVM(api: AdminApi, userId: string): UserDetailVM {
  const state = $state<UserDetailState>({
    user: null,
    loading: false,
    error: null,
    quotaInput: "",
    saving: false,
    saved: false,
    quotaError: null,
  });

  const breakdown = $derived.by((): UsageSlice[] => {
    const user = state.user;
    if (!user) return [];
    // Against the charged total, not the quota: this bar answers "what is my
    // space going on", which the quota bar does not.
    const total = Math.max(1, user.used_bytes);
    return [
      { label: "Live", bytes: user.live_bytes, share: (100 * user.live_bytes) / total },
      { label: "Trash", bytes: user.trashed_bytes, share: (100 * user.trashed_bytes) / total },
      {
        label: "Versions",
        bytes: user.version_bytes,
        share: (100 * user.version_bytes) / total,
      },
    ];
  });

  const canSave = $derived(
    !state.saving && parseQuota(state.quotaInput) !== null && state.user !== null,
  );

  async function load(): Promise<void> {
    state.loading = true;
    state.error = null;
    try {
      state.user = await api.user(userId);
      state.quotaInput = String(state.user.quota_bytes);
    } catch (err) {
      state.error = describeError(err);
    } finally {
      state.loading = false;
    }
  }

  function setQuotaInput(value: string): void {
    state.quotaInput = value;
    state.quotaError = null;
    state.saved = false;
  }

  async function saveQuota(): Promise<void> {
    const bytes = parseQuota(state.quotaInput);
    if (bytes === null) {
      state.quotaError = "Enter a size, for example 10 GB.";
      return;
    }

    state.saving = true;
    state.quotaError = null;
    try {
      // The response is the recomputed figures, so `over_quota` reflects the
      // new limit immediately without a second request.
      state.user = await api.setQuota(userId, bytes);
      state.quotaInput = String(state.user.quota_bytes);
      state.saved = true;
    } catch (err) {
      state.quotaError = describeError(err);
    } finally {
      state.saving = false;
    }
  }

  return {
    get state() {
      return state;
    },
    get breakdown() {
      return breakdown;
    },
    get canSave() {
      return canSave;
    },
    load,
    setQuotaInput,
    saveQuota,
  };
}
