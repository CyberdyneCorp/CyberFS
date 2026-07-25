<script lang="ts">
  import type { Snippet } from "svelte";

  interface Props {
    title: string;
    description?: string;
    actions?: Snippet;
  }

  const { title, description, actions }: Props = $props();
</script>

<header class="page-header">
  <div>
    <h1>{title}</h1>
    {#if description}
      <p>{description}</p>
    {/if}
  </div>
  {#if actions}
    <div class="toolbar" style="margin: 0">{@render actions()}</div>
  {/if}
</header>
