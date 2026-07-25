// Composition root.
//
// Everything wired here so components import one module and never construct a
// client, and so the lint rule that bans `$lib/api/client` inside `.svelte`
// files leaves nothing awkward behind.

import { ApiClient } from "$lib/api/client";
import { createAdminApi, type AdminApi } from "$lib/api/endpoints";
import { AuthClient, type Profile } from "$lib/auth/auth-client";
import {
  clear,
  createTokenSource,
  rememberReturnPath,
  session,
  setTokens,
  useStore,
  browserStore,
  restore,
} from "$lib/auth/session.svelte";
import { appConfig } from "$lib/config";

interface Wiring {
  api: AdminApi;
  auth: AuthClient;
}

let wiring: Wiring | null = null;
let authenticationLost: () => void = () => {};

/** The layout registers navigation here; nothing lower down redirects. */
export function onAuthenticationLost(handler: () => void): void {
  authenticationLost = handler;
}

function wire(): Wiring {
  if (wiring) return wiring;

  const config = appConfig();
  const auth = new AuthClient(config.authUrl);
  const tokens = createTokenSource({ refresh: (token) => auth.refresh(token) }, () =>
    authenticationLost(),
  );
  const api = createAdminApi(new ApiClient({ baseUrl: config.apiUrl, tokens }));

  wiring = { api, auth };
  return wiring;
}

export function api(): AdminApi {
  return wire().api;
}

/** Called once by the root layout, before anything reads the session. */
export function initSession(): void {
  useStore(browserStore());
  restore();
}

/**
 * Sends the browser to CyberdyneAuth.
 *
 * The current path is remembered first so a deep link survives the round trip
 * through the identity provider.
 */
export async function beginLogin(returnPath: string): Promise<void> {
  const config = appConfig();
  rememberReturnPath(returnPath);
  const redirect = `${window.location.origin}/auth/callback`;
  const url = await wire().auth.authorizationUrl(config.provider, redirect);
  window.location.assign(url);
}

export function adoptTokens(accessToken: string, refreshToken: string): void {
  setTokens(accessToken, refreshToken);
}

/**
 * Resolves who the token belongs to and whether they may use this dashboard.
 *
 * `is_admin` is CyberdyneAuth's answer, not ours -- the same flag the API
 * enforces on every admin request, so the UI and the server cannot disagree.
 */
export async function loadProfile(): Promise<Profile | null> {
  const token = session.accessToken;
  if (!token) return null;
  const profile = await wire().auth.profile(token);
  session.subject = profile.email ?? profile.id;
  session.isAdmin = profile.is_admin;
  session.checked = true;
  return profile;
}

export function signOut(): void {
  clear();
}
