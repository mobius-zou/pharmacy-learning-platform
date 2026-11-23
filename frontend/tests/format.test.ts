import { describe, expect, it } from "vitest";
import { formatDays, formatPercent, masteryLabel, masteryLevel, tagSummary } from "@/shared/lib/format";

describe("掌握度色阶", () => {
  it("按稳定度分档", () => {
    expect(masteryLevel(null)).toBe(1);
    expect(masteryLevel(0)).toBe(1);
    expect(masteryLevel(1)).toBe(1);
    expect(masteryLevel(3)).toBe(2);
    expect(masteryLevel(13.9)).toBe(2);
    expect(masteryLevel(14)).toBe(3);
    expect(masteryLevel(45)).toBe(4);
  });

  it("标签与档位一一对应", () => {
    expect(masteryLabel(null)).toBe("初识");
    expect(masteryLabel(5)).toBe("熟悉");
    expect(masteryLabel(20)).toBe("稳固");
    expect(masteryLabel(90)).toBe("精通");
  });
});

describe("格式化", () => {
  it("间隔天数在合理区间内可读", () => {
    expect(formatDays(0.5)).toContain("分钟");
    expect(formatDays(3)).toContain("天");
    expect(formatDays(60)).toContain("个月");
    expect(formatDays(null)).toBe("—");
  });

  it("百分比收敛到 0..100，非法值显示破折号", () => {
    expect(formatPercent(0.856)).toBe("86%");
    expect(formatPercent(1.4)).toBe("100%");
    expect(formatPercent(-1)).toBe("0%");
    expect(formatPercent(null)).toBe("—");
  });

  it("标签按章节优先并限制数量", () => {
    const tags = { subjects: ["药理学"], chapters: ["解热镇痛药", "COX"], point_types: ["机制"] };
    expect(tagSummary(tags, 2)).toEqual(["解热镇痛药", "COX"]);
    expect(tagSummary(undefined)).toEqual([]);
    expect(tagSummary({ chapters: ["只有一个"] })).toEqual(["只有一个"]);
  });
});
