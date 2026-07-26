// One place that turns a thrown thing into a sentence an operator can act on.
//
// View models call this rather than formatting errors themselves, so the same
// failure reads the same way everywhere.

import {
  ApiError,
  ForbiddenError,
  NetworkError,
  RateLimitError,
  UnauthorizedError,
} from "$lib/api/client";

export function describeError(err: unknown): string {
  if (err instanceof NetworkError) {
    // Name the service that actually failed. A browser reports a CORS or CSP
    // rejection as an unreachable host, so a running service can still land
    // here -- point at the allowlists rather than only at "is it up".
    return (
      `Could not reach ${err.service}. Check that it is running and that this ` +
      `origin is allowed by its CORS policy.`
    );
  }
  if (err instanceof UnauthorizedError) {
    return "Your session has expired. Sign in again.";
  }
  if (err instanceof ForbiddenError) {
    return "You do not have administrator access.";
  }
  if (err instanceof RateLimitError) {
    return err.retryAfterSeconds
      ? `Too many requests. Try again in ${err.retryAfterSeconds}s.`
      : "Too many requests. Try again shortly.";
  }
  if (err instanceof ApiError) {
    // The API's problem detail is already written for a human.
    return err.problem?.detail ?? `Request failed (${err.status}).`;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return "Something went wrong.";
}

/** Whether a failure means the session is over, so the layout can redirect. */
export function isAuthenticationLost(err: unknown): boolean {
  return err instanceof UnauthorizedError;
}
