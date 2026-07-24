<script lang="ts">
  import type { Snippet } from "svelte";

  // Loading, error, and empty rendered in one place so every route says the
  // same thing the same way. `aria-live` announces the transition rather than
  // leaving a screen-reader user on a silent page.
  interface Props {
    loading: boolean;
    error: string | null;
    empty?: boolean;
    emptyMessage?: string;
    onRetry?: () => void;
    children?: Snippet;
  }

  const {
    loading,
    error,
    empty = false,
    emptyMessage = "Nothing to show yet.",
    onRetry,
    children,
  }: Props = $props();
</script>

<div aria-live="polite" aria-busy={loading}>
  {#if error}
    <p class="notice error" role="alert">
      {error}
      {#if onRetry}
        <button type="button" onclick={onRetry}>Try again</button>
      {/if}
    </p>
  {:else if loading}
    <p class="empty">Loading…</p>
  {:else if empty}
    <p class="empty">{emptyMessage}</p>
  {:else if children}
    {@render children()}
  {/if}
</div>
