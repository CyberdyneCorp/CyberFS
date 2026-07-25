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
    return "Could not reach CyberFS. Check that the API is running.";
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
