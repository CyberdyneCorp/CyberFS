import adapter from "@sveltejs/adapter-node";
import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

/** @type {import('@sveltejs/kit').Config} */
const config = {
  preprocess: vitePreprocess(),
  kit: {
    adapter: adapter({ precompress: false }),
    alias: { $lib: "./src/lib" },
    // SvelteKit owns CSP and hashes its own inline scripts. `connect-src` has
    // to name both origins the dashboard talks to: CyberFS for data, and
    // CyberdyneAuth for the login handshake and token refresh.
    csp: {
      mode: "hash",
      directives: {
        "default-src": ["self"],
        "script-src": ["self"],
        "style-src": ["self", "unsafe-inline"],
        "img-src": ["self", "data:"],
        "font-src": ["self", "data:"],
        "connect-src": [
          "self",
          "http://localhost:8000",
          "http://localhost:8001",
          "https://cyberfs.backend.coolify.cyberdynecorp.ai",
          "https://auth.backend.coolify.cyberdynecorp.ai",
        ],
        "frame-ancestors": ["none"],
        "base-uri": ["self"],
        "form-action": ["self"],
        "object-src": ["none"],
      },
    },
  },
};

export default config;
