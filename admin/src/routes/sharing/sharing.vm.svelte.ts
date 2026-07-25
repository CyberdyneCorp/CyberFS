// Public links, and revoking them.
//
// Revocation is destructive and instant, so it goes through an explicit
// confirmation step held here rather than a `confirm()` in the component.

import type { AdminApi } from "$lib/api/endpoints";
import type { PublicLink } from "$lib/api/types";
import { describeError } from "$lib/errors";

export interface SharingState {
  links: PublicLink[];
  loading: boolean;
  error: string | null;
  /** The link awaiting confirmation, if any. */
  pendingRevoke: string | null;
  revoking: string | null;
  notice: string | null;
}

export interface SharingVM {
  readonly state: SharingState;
  readonly expiringSoon: PublicLink[];
  readonly unprotectedCount: number;
  load(): Promise<void>;
  askRevoke(linkId: string): void;
  cancelRevoke(): void;
  confirmRevoke(): Promise<void>;
}

const SOON_MS = 7 * 86_400_000;

export function isExpiringSoon(link: PublicLink, now: Date = new Date()): boolean {
  if (!link.expires_at) return false;
  const expiry = new Date(link.expires_at).getTime();
  if (Number.isNaN(expiry)) return false;
  const remaining = expiry - now.getTime();
  return remaining > 0 && remaining <= SOON_MS;
}

const systemClock = (): Date => new Date();

export function createSharingVM(api: AdminApi, clock: () => Date = systemClock): SharingVM {
  const state = $state<SharingState>({
    links: [],
    loading: false,
    error: null,
    pendingRevoke: null,
    revoking: null,
    notice: null,
  });

  const expiringSoon = $derived(state.links.filter((link) => isExpiringSoon(link, clock())));
  // A link with no passphrase is readable by anyone holding the URL, which is
  // the thing an operator reviewing exposure most wants to spot.
  const unprotectedCount = $derived(
    state.links.filter((link) => !link.passphrase_protected).length,
  );

  async function load(): Promise<void> {
    state.loading = true;
    state.error = null;
    try {
      state.links = (await api.links(200)).items;
    } catch (err) {
      state.error = describeError(err);
    } finally {
      state.loading = false;
    }
  }

  function askRevoke(linkId: string): void {
    state.pendingRevoke = linkId;
    state.notice = null;
  }

  function cancelRevoke(): void {
    state.pendingRevoke = null;
  }

  async function confirmRevoke(): Promise<void> {
    const linkId = state.pendingRevoke;
    if (!linkId) return;

    state.revoking = linkId;
    state.pendingRevoke = null;
    try {
      await api.revokeLink(linkId);
      // Drop it locally rather than refetching: the operator is often working
      // through a list, and a reload would lose their place.
      state.links = state.links.filter((link) => link.id !== linkId);
      state.notice = "Link revoked. It stopped working immediately.";
    } catch (err) {
      state.error = describeError(err);
    } finally {
      state.revoking = null;
    }
  }

  return {
    get state() {
      return state;
    },
    get expiringSoon() {
      return expiringSoon;
    },
    get unprotectedCount() {
      return unprotectedCount;
    },
    load,
    askRevoke,
    cancelRevoke,
    confirmRevoke,
  };
}
