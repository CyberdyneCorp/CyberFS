// Parsing the OAuth callback fragment.
//
// The dashboard is a *different origin* from CyberdyneAuth, so its callback
// cannot set cookies we can read. CyberdyneAuth's documented answer for this
// case is fragment mode: it redirects to our callback with the tokens in the
// URL fragment. Fragments are never sent to a server and are not written to
// proxy or access logs, which is why the tokens travel there rather than in the
// query string.
//
// Pure and DOM-free, so the parsing rules can be tested directly.

export interface OAuthFragment {
  accessToken: string;
  refreshToken: string;
  tokenType: string;
  expiresIn: number;
  isNewUser: boolean;
}

export function parseOAuthFragment(hash: string): OAuthFragment | null {
  if (!hash.startsWith("#")) return null;

  const params = new URLSearchParams(hash.slice(1));
  const accessToken = params.get("access_token");
  const refreshToken = params.get("refresh_token");
  // Both are required: a half-populated fragment is a broken handshake, not a
  // partial session.
  if (!accessToken || !refreshToken) return null;

  const expiresIn = Number.parseInt(params.get("expires_in") ?? "0", 10);
  return {
    accessToken,
    refreshToken,
    tokenType: params.get("token_type") ?? "Bearer",
    expiresIn: Number.isFinite(expiresIn) ? expiresIn : 0,
    isNewUser: params.get("is_new_user") === "true",
  };
}

/** An OAuth error handed back in the fragment, if the handshake failed. */
export function parseOAuthError(hash: string): string | null {
  if (!hash.startsWith("#")) return null;
  const params = new URLSearchParams(hash.slice(1));
  const error = params.get("error");
  if (!error) return null;
  return params.get("error_description") ?? error;
}

/**
 * Where to send the browser after a successful login.
 *
 * Only same-site paths are honoured: an absolute URL or a protocol-relative
 * one would turn the login flow into an open redirect.
 */
export function safeReturnPath(candidate: string | null, fallback = "/"): string {
  if (!candidate) return fallback;
  if (!candidate.startsWith("/")) return fallback;
  if (candidate.startsWith("//")) return fallback;
  return candidate;
}
