import js from "@eslint/js";
import svelte from "eslint-plugin-svelte";
import globals from "globals";
import ts from "typescript-eslint";

/**
 * The rule that matters here is the last one: `.svelte` files must contain no
 * network calls and no business logic. `admin-dashboard/spec.md` requires data
 * loading, filtering, sorting, pagination and error handling to live in a
 * route's view model so it can be tested without a DOM. A lint rule enforces
 * that rather than leaving it to review.
 */
export default ts.config(
  js.configs.recommended,
  ...ts.configs.recommended,
  ...svelte.configs["flat/recommended"],
  {
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
  {
    files: ["**/*.svelte"],
    languageOptions: {
      parserOptions: { parser: ts.parser },
    },
    rules: {
      "no-restricted-globals": [
        "error",
        {
          name: "fetch",
          message:
            "Components must not call the network. Put the call in the route's " +
            "*.vm.svelte.ts view model, which reaches the API through $lib/api.",
        },
      ],
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["$lib/api/client", "**/api/client"],
              message:
                "Components must not touch the API client directly. Go through " +
                "the route's view model.",
            },
          ],
        },
      ],
    },
  },
  {
    ignores: ["node_modules/", ".svelte-kit/", "build/", "playwright-report/", "test-results/"],
  },
);
