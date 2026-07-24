<script lang="ts">
  // A proportion bar. Given a label it becomes a progress meter for assistive
  // technology; without one it is decoration beside a number that already says
  // the same thing, so it is hidden instead of read out twice.
  interface Props {
    percent: number;
    severity?: "ok" | "warn" | "over";
    label?: string;
  }

  const { percent, severity = "ok", label }: Props = $props();
  const clamped = $derived(Math.max(0, Math.min(100, percent)));
</script>

{#if label}
  <div
    class="bar {severity}"
    role="progressbar"
    aria-label={label}
    aria-valuenow={Math.round(clamped)}
    aria-valuemin="0"
    aria-valuemax="100"
  >
    <span style="width: {clamped}%"></span>
  </div>
{:else}
  <div class="bar {severity}" aria-hidden="true">
    <span style="width: {clamped}%"></span>
  </div>
{/if}
