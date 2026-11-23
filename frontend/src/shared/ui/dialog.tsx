import { useEffect, useRef } from "react";
import type { ReactNode } from "react";
import { Button, cx } from "./primitives";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "primary" | "danger";
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * 二次确认弹窗（原生 <dialog>：自带焦点捕获、Esc 关闭）。
 *
 * 只用于**不可逆/有副作用**的操作：评分、确认入库、拒绝草稿。
 * 普通操作不弹窗 —— 过度确认会打断心流。
 */
export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "确认",
  cancelLabel = "取消",
  tone = "primary",
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const ref = useRef<HTMLDialogElement | null>(null);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    const handleCancel = (event: Event) => {
      event.preventDefault();
      if (!loading) onCancel();
    };
    dialog.addEventListener("cancel", handleCancel);
    return () => dialog.removeEventListener("cancel", handleCancel);
  }, [loading, onCancel]);

  return (
    <dialog ref={ref} className={cx("pl-dialog")} aria-labelledby="pl-dialog-title">
      <div className="pl-dialog__body">
        <h2 id="pl-dialog-title" className="pl-dialog__title">
          {title}
        </h2>
        {description ? <div className="pl-dialog__desc">{description}</div> : null}
      </div>
      <div className="pl-dialog__actions">
        <Button variant="ghost" onClick={onCancel} disabled={loading}>
          {cancelLabel}
        </Button>
        <Button
          variant={tone === "danger" ? "danger" : "primary"}
          onClick={onConfirm}
          loading={loading}
          autoFocus
        >
          {confirmLabel}
        </Button>
      </div>
    </dialog>
  );
}
