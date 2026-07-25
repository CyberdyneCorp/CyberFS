// A single-page app: the session lives in `sessionStorage`, which the server
// cannot see, so rendering on the server would only produce a signed-out shell
// that the client immediately replaces.
export const ssr = false;
export const prerender = false;
