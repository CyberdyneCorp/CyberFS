<script lang="ts">
  interface Props {
    label: string;
    value: string;
    hint?: string;
  }

  const { label, value, hint }: Props = $props();
</script>

<div class="card">
  <dl class="stat">
    <dt>{label}</dt>
    <!-- The hint lives inside the <dd>: a <dl> may only contain <dt>/<dd>
         groups, and axe reports a stray sibling as a serious violation. -->
    <dd>
      {value}
      {#if hint}<small>{hint}</small>{/if}
    </dd>
  </dl>
</div>
