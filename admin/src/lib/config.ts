// Runtime configuration.
//
// Read from `$env/dynamic/public` rather than `$env/static/public` so one built
// image can be pointed at dev, staging, or production by environment alone --
// which is how Coolify deploys the rest of Cyberdyne.

import { env } from "$env/dynamic/public";

export interface AppConfig {
  /** CyberFS API origin. */
  apiUrl: string;
  /** CyberdyneAuth origin. */
  authUrl: string;
  /** Which OAuth provider the login button starts. */
  provider: string;
}

export function appConfig(): AppConfig {
  return {
    apiUrl: trimSlash(env.PUBLIC_CYBERFS_API_URL ?? "http://localhost:8000"),
    authUrl: trimSlash(env.PUBLIC_CYBERDYNE_AUTH_URL ?? "http://localhost:8001"),
    provider: env.PUBLIC_OAUTH_PROVIDER ?? "google",
  };
}

function trimSlash(url: string): string {
  return url.replace(/\/+$/, "");
}
