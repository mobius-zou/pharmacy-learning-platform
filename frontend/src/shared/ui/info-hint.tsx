import { useId, useState } from "react";
import type { ReactNode } from "react";
import { cx } from "./primitives";

/**
 * 术语解释：专业词（FSRS / 薄弱点 / PICO-D / 三元组 / 优先级）首次出现时
 * 给一句话说明。默认收起，点击或聚焦展开 —— 不把解释堆在页面上。
 */
export function InfoHint({
  term,
  explanation,
  className,
}: {
  term: string;
  explanation: ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();

  return (
    <span className={cx("pl-hint", className)}>
      <button
        type="button"
        className="pl-hint__trigger"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
        onBlur={() => setOpen(false)}
        title={`${term}：展开解释`}
      >
        <span className="pl-hint__term">{term}</span>
        <span className="pl-hint__mark" aria-hidden="true">
          ?
        </span>
      </button>
      {open ? (
        <span role="tooltip" id={panelId} className="pl-hint__panel">
          {explanation}
        </span>
      ) : null}
    </span>
  );
}
