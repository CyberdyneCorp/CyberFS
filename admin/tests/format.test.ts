import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatDuration,
  formatPercent,
  formatRelative,
  humanizeAction,
  quotaSeverity,
} from "$lib/format";

describe("formatBytes", () => {
  it("reports small values exactly", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(512)).toBe("512 B");
  });

  it("steps up through the units", () => {
    expect(formatBytes(1024)).toBe("1.0 KB");
    expect(formatBytes(1024 ** 2)).toBe("1.0 MB");
    expect(formatBytes(1024 ** 3)).toBe("1.0 GB");
    expect(formatBytes(1024 ** 4)).toBe("1.0 TB");
  });

  it("does not run out of units", () => {
    expect(formatBytes(1024 ** 6)).toContain("PB");
  });

  it("treats nonsense as zero rather than rendering NaN", () => {
    expect(formatBytes(Number.NaN)).toBe("0 B");
    expect(formatBytes(-5)).toBe("0 B");
  });
});

describe("formatPercent", () => {
  it("does not round a nonzero share down to 0%", () => {
    // A user with a few bytes against a 10 GB quota is not at zero.
    expect(formatPercent(0.004)).toBe("<0.1%");
  });

  it("is exactly zero only when it is zero", () => {
    expect(formatPercent(0)).toBe("0.0%");
  });

  it("drops the decimal once the number is large", () => {
    expect(formatPercent(4.25)).toBe("4.3%");
    expect(formatPercent(87.4)).toBe("87%");
  });
});

describe("formatRelative", () => {
  const now = new Date("2026-07-24T12:00:00Z");

  it("says never when there is no timestamp", () => {
    expect(formatRelative(null, now)).toBe("never");
  });

  it("describes recent times", () => {
    expect(formatRelative("2026-07-24T11:59:30Z", now)).toBe("just now");
    expect(formatRelative("2026-07-24T11:30:00Z", now)).toBe("30m ago");
    expect(formatRelative("2026-07-24T09:00:00Z", now)).toBe("3h ago");
    expect(formatRelative("2026-07-20T12:00:00Z", now)).toBe("4d ago");
  });

  it("does not crash on a malformed timestamp", () => {
    expect(formatRelative("not-a-date", now)).toBe("unknown");
  });
});

describe("formatDuration", () => {
  it("uses milliseconds below a second", () => {
    expect(formatDuration(0.25)).toBe("250 ms");
  });

  it("uses minutes above one", () => {
    expect(formatDuration(95)).toBe("1m 35s");
  });

  it("renders a dash when a job has never run", () => {
    expect(formatDuration(null)).toBe("—");
  });
});

describe("quotaSeverity", () => {
  it("warns before the limit is reached", () => {
    expect(quotaSeverity(50, false)).toBe("ok");
    expect(quotaSeverity(85, false)).toBe("warn");
  });

  it("reports over-quota regardless of the percentage", () => {
    // Lowering a quota below usage puts a user over at any percentage.
    expect(quotaSeverity(5, true)).toBe("over");
  });
});

describe("humanizeAction", () => {
  it("turns an action code into a readable label", () => {
    expect(humanizeAction("grant.created")).toBe("Grant created");
    expect(humanizeAction("admin.quota_changed")).toBe("Admin quota changed");
  });
});
