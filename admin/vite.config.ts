import { sveltekit } from "@sveltejs/kit/vite";
import { defineConfig, type UserConfig } from "vite";

// vitest's `test` key is not part of Vite's UserConfig; extend it here so one
// config serves both bundling and `vitest run`.
interface VitestAwareConfig extends UserConfig {
  test?: {
    environment?: string;
    globals?: boolean;
    include?: string[];
    exclude?: string[];
  };
}

export default defineConfig({
  plugins: [sveltekit()],
  server: { port: 3002, host: "0.0.0.0", strictPort: true },
  test: {
    environment: "jsdom",
    globals: true,
    // View models are tested here; Playwright owns e2e/ and accessibility.
    include: ["tests/**/*.test.ts"],
    exclude: ["e2e/**", "node_modules/**", ".svelte-kit/**", "build/**"],
  },
} as VitestAwareConfig);
