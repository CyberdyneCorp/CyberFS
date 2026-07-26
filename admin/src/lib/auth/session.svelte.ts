// The session: what token we hold, who it belongs to, and whether they are an
// administrator.
//
// Tokens live in `sessionStorage`, not `localStorage`: they should not outlive
// the tab, and a shared machine should not carry a session between users.

import { parseOAuthFragment, safeReturnPath } from "$lib/auth/fragment";
import type { TokenSource } from "$lib/api/client";

const ACCESS_KEY = "cyberfs.access_token";
const REFRESH_KEY = "cyberfs.refresh_token";
const RETURN_KEY = "cyberfs.return_to";

export interface SessionState {
  accessToken: string | null;
  refreshToken: string | null;
  subject: string | null;
  isAdmin: boolean;
  /** Null until the admin probe has run, so the UI can tell "unknown" from "no". */
  checked: boolean;
  error: string | null;
}

export const session = $state<SessionState>({
  accessToken: null,
  refreshToken: null,
  subject: null,
  isAdmin: false,
  checked: false,
  error: null,
});

/** Swappable so tests never touch a real Storage. */
export interface Store {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const memoryStore: Store = (() => {
  const map = new Map<string, string>();
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    removeItem: (k) => void map.delete(k),
  };
})();

let store: Store = memoryStore;

export function useStore(replacement: Store): void {
  store = replacement;
}

export function browserStore(): Store {
  // Server-side rendering has no sessionStorage; fall back rather than throw.
  return typeof sessionStorage === "undefined" ? memoryStore : sessionStorage;
}

// --- lifecycle -------------------------------------------------------------

export function restore(): void {
  session.accessToken = store.getItem(ACCESS_KEY);
  session.refreshToken = store.getItem(REFRESH_KEY);
}

/** Adopt the tokens from an OAuth callback fragment. */
export function adopt(hash: string): boolean {
  const parsed = parseOAuthFragment(hash);
  if (!parsed) return false;
  setTokens(parsed.accessToken, parsed.refreshToken);
  return true;
}

export function setTokens(accessToken: string, refreshToken: string): void {
  session.accessToken = accessToken;
  session.refreshToken = refreshToken;
  session.error = null;
  store.setItem(ACCESS_KEY, accessToken);
  store.setItem(REFRESH_KEY, refreshToken);
}

export function clear(): void {
  session.accessToken = null;
  session.refreshToken = null;
  session.subject = null;
  session.isAdmin = false;
  session.checked = false;
  store.removeItem(ACCESS_KEY);
  store.removeItem(REFRESH_KEY);
}

export function rememberReturnPath(path: string): void {
  // The sign-in pages are never a destination. The layout stores the real
  // deep link before redirecting here, and both sign-in paths then ask to
  // remember where they are -- which would clobber it and land the operator
  // back on the login page after authenticating.
  if (isSignInRoute(path)) return;
  store.setItem(RETURN_KEY, path);
}

export function takeReturnPath(): string {
  const saved = store.getItem(RETURN_KEY);
  store.removeItem(RETURN_KEY);
  // Guard the read too: a stored sign-in route would be a redirect loop.
  return isSignInRoute(saved) ? "/" : safeReturnPath(saved);
}

function isSignInRoute(path: string | null): boolean {
  if (!path) return false;
  const bare = path.split("?")[0].replace(/\/+$/, "");
  return bare === "/login" || bare === "/auth/callback";
}

// --- token source ----------------------------------------------------------

export interface AuthEndpoints {
  refresh(refreshToken: string): Promise<{ access_token: string; refresh_token: string }>;
}

/**
 * Adapts the session to what `ApiClient` needs.
 *
 * `onAuthenticationLost` clears rather than redirecting: navigation is the
 * layout's job, and a module that redirects on its own is impossible to test.
 */
export function createTokenSource(auth: AuthEndpoints, onLost: () => void): TokenSource {
  return {
    accessToken: () => session.accessToken,

    refresh: async () => {
      const token = session.refreshToken;
      if (!token) return false;
      try {
        const pair = await auth.refresh(token);
        setTokens(pair.access_token, pair.refresh_token);
        return true;
      } catch {
        // A refresh token that no longer works means the session is over.
        return false;
      }
    },

    onAuthenticationLost: () => {
      clear();
      onLost();
    },
  };
}
