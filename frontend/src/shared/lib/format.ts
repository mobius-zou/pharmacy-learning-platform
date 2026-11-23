/** 展示层格式化：只做「看得懂」的转换，不改动语义。 */

const DAY_MS = 86_400_000;

export function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDate(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

export function formatDateTime(value: string | null | undefined): string {
  const date = parseDate(value);
  if (!date) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

/** 相对时间：用于「上次复习 / 到期」这类需要快速感知的场景。 */
export function formatRelative(value: string | null | undefined, now = new Date()): string {
  const date = parseDate(value);
  if (!date) return "—";
  const diffMs = date.getTime() - now.getTime();
  const abs = Math.abs(diffMs);
  const formatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });

  if (abs < 60_000) return "刚刚";
  if (abs < 3_600_000) return formatter.format(Math.round(diffMs / 60_000), "minute");
  if (abs < DAY_MS) return formatter.format(Math.round(diffMs / 3_600_000), "hour");
  if (abs < DAY_MS * 30) return formatter.format(Math.round(diffMs / DAY_MS), "day");
  if (abs < DAY_MS * 365) return formatter.format(Math.round(diffMs / (DAY_MS * 30)), "month");
  return formatter.format(Math.round(diffMs / (DAY_MS * 365)), "year");
}

/** 稳定度（FSRS stability，单位天）→ 掌握度色阶 1..4，用于渐变指示。 */
export function masteryLevel(stability: number | null): 1 | 2 | 3 | 4 {
  if (stability === null || stability <= 0) return 1;
  if (stability < 3) return 1;
  if (stability < 14) return 2;
  if (stability < 45) return 3;
  return 4;
}

export function masteryLabel(stability: number | null): string {
  const level = masteryLevel(stability);
  return ["初识", "熟悉", "稳固", "精通"][level - 1] ?? "初识";
}

/** 0..1 → 百分比字符串，NaN/越界都收敛到合法范围。 */
export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const clamped = Math.min(1, Math.max(0, value));
  return `${(clamped * 100).toFixed(digits)}%`;
}

export function formatDays(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (value < 1) return `${Math.round(value * 24 * 60)} 分钟`;
  if (value < 30) return `${value.toFixed(1)} 天`;
  return `${(value / 30).toFixed(1)} 个月`;
}

/** 把标签按维度拍平成可读片段（章节优先，其次科目、考点类型）。 */
export function tagSummary(tags: Record<string, unknown> | undefined, limit = 3): string[] {
  if (!tags) return [];
  const result: string[] = [];
  for (const key of ["chapters", "subjects", "point_types"] as const) {
    const value = tags[key];
    if (!Array.isArray(value)) continue;
    for (const item of value) {
      if (typeof item === "string" && item.trim()) result.push(item.trim());
      if (result.length >= limit) return result;
    }
  }
  return result;
}
