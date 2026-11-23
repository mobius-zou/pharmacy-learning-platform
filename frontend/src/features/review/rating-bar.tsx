import type { PreviewOption, Rating } from "@/shared/api/types";
import { RATING_HINTS, RATING_LABELS } from "./session";
import { cx } from "@/shared/ui/primitives";

const TONE: Record<Rating, string> = {
  1: "is-recall-miss",
  2: "is-hard",
  3: "is-good",
  4: "is-easy",
};

export interface RatingBarProps {
  options: PreviewOption[] | undefined;
  loading: boolean;
  disabled: boolean;
  onChoose: (rating: Rating) => void;
}

/**
 * 四档评分：颜色 + 文字双重编码（不只靠颜色区分），
 * 并展示每档对应的下次间隔 —— 让学习者对后果「心里有数」。
 * 快捷键 1–4 由父组件统一监听。
 */
export function RatingBar({ options, loading, disabled, onChoose }: RatingBarProps) {
  const byRating = new Map<number, PreviewOption>();
  for (const option of options ?? []) byRating.set(option.rating, option);

  return (
    <div className="pl-ratings" role="group" aria-label="选择回忆程度">
      {([1, 2, 3, 4] as Rating[]).map((rating) => {
        const option = byRating.get(rating);
        return (
          <button
            key={rating}
            type="button"
            className={cx("pl-rating", TONE[rating])}
            disabled={disabled}
            onClick={() => onChoose(rating)}
            aria-label={`${RATING_LABELS[rating]}（快捷键 ${rating}）${
              option ? `，下次间隔 ${option.interval_label}` : ""
            }`}
            title={RATING_HINTS[rating]}
          >
            <span className="pl-rating__key" aria-hidden="true">
              {rating}
            </span>
            <span className="pl-rating__label">{RATING_LABELS[rating]}</span>
            <span className="pl-rating__interval">
              {loading ? "…" : (option?.interval_label ?? "—")}
            </span>
          </button>
        );
      })}
    </div>
  );
}
