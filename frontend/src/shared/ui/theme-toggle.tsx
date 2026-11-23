import { useEffect, useState } from "react";
import { cx } from "./primitives";
import {
  applyTheme,
  readThemePreference,
  resolveTheme,
  saveThemePreference,
  type ThemePreference,
} from "../lib/theme";

const ORDER: ThemePreference[] = ["system", "light", "dark"];
const LABEL: Record<ThemePreference, string> = { system: "跟随系统", light: "浅色", dark: "深色" };

/** 主题切换：三态循环（跟随系统 → 浅色 → 深色）。 */
export function ThemeToggle({ className }: { className?: string }) {
  const [preference, setPreference] = useState<ThemePreference>(readThemePreference);

  useEffect(() => {
    applyTheme(resolveTheme(preference));
    saveThemePreference(preference);
    if (preference !== "system" || !window.matchMedia) return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => applyTheme(resolveTheme("system"));
    media.addEventListener("change", onChange);
    return () => media.removeEventListener("change", onChange);
  }, [preference]);

  const nextPreference = ORDER[(ORDER.indexOf(preference) + 1) % ORDER.length] ?? "system";

  return (
    <button
      type="button"
      className={cx("pl-theme-toggle", className)}
      onClick={() => setPreference(nextPreference)}
      aria-label={`主题：${LABEL[preference]}，点击切换为${LABEL[nextPreference]}`}
      title={`主题：${LABEL[preference]}`}
    >
      <span aria-hidden="true" className="pl-theme-toggle__icon">
        {preference === "dark" ? "☾" : preference === "light" ? "☀" : "◐"}
      </span>
      <span className="pl-theme-toggle__label">{LABEL[preference]}</span>
    </button>
  );
}
