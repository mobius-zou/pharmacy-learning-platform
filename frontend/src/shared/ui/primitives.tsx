import { forwardRef } from "react";
import type {
  ButtonHTMLAttributes,
  HTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  TextareaHTMLAttributes,
} from "react";

/** 极小的 class 合并工具（不引入 clsx，保持零额外依赖）。 */
export function cx(...values: Array<string | false | null | undefined>): string {
  return values.filter(Boolean).join(" ");
}

/* ------------------------------- Button ------------------------------- */

type Variant = "primary" | "soft" | "ghost" | "danger";
type Size = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  loading?: boolean;
  block?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", loading = false, block = false, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cx("pl-btn", `pl-btn--${variant}`, `pl-btn--${size}`, block && "pl-btn--block", className)}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <span className="pl-btn__spinner" aria-hidden="true" /> : null}
      <span className="pl-btn__label">{children}</span>
    </button>
  );
});

/* ------------------------------- Panel -------------------------------- */

export interface PanelProps extends HTMLAttributes<HTMLDivElement> {
  tone?: "default" | "soft" | "accent";
  as?: "div" | "section" | "article";
}

export function Panel({ tone = "default", as = "div", className, children, ...rest }: PanelProps) {
  const Tag = as;
  return (
    <Tag className={cx("pl-panel", `pl-panel--${tone}`, className)} {...rest}>
      {children}
    </Tag>
  );
}

/* ------------------------------- Field -------------------------------- */

export interface FieldProps {
  label: string;
  htmlFor: string;
  hint?: ReactNode;
  error?: string | null;
  required?: boolean;
  children: ReactNode;
}

export function Field({ label, htmlFor, hint, error, required, children }: FieldProps) {
  return (
    <div className="pl-field">
      {/*
        必填标记用 CSS ::after 呈现，而不是塞进 <label> 的文本节点：
        否则可访问名会变成「密码 *」，getByLabel 之类按精确名匹配就会失效。
      */}
      <label className="pl-field__label" htmlFor={htmlFor} data-required={required || undefined}>
        {label}
      </label>
      {children}
      {hint ? (
        <p className="pl-field__hint" id={`${htmlFor}-hint`}>
          {hint}
        </p>
      ) : null}
      {error ? (
        <p className="pl-field__error" id={`${htmlFor}-error`} role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...rest }, ref) {
    return <input ref={ref} className={cx("pl-input", className)} {...rest} />;
  },
);

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...rest }, ref) {
    return <textarea ref={ref} className={cx("pl-input", "pl-textarea", className)} {...rest} />;
  },
);

/* ------------------------------- Badge -------------------------------- */

export interface BadgeProps {
  tone?: "neutral" | "primary" | "ok" | "warn" | "danger" | "info";
  children: ReactNode;
  className?: string;
}

export function Badge({ tone = "neutral", children, className }: BadgeProps) {
  return <span className={cx("pl-badge", `pl-badge--${tone}`, className)}>{children}</span>;
}

/* ------------------------------ Progress ------------------------------ */

export interface ProgressProps {
  value: number;
  max: number;
  label: string;
  tone?: "primary" | "ok";
}

export function Progress({ value, max, label, tone = "primary" }: ProgressProps) {
  // 用原生 <progress>：宽度由浏览器按 value/max 绘制，
  // 不需要内联 style —— 这是能启用严格 CSP（style-src 'self'）的前提。
  return (
    <progress
      className={cx("pl-progress", `pl-progress--${tone}`)}
      value={Math.max(0, Math.min(value, max))}
      max={max > 0 ? max : 1}
      aria-label={label}
    />
  );
}

/* ------------------------------ Skeleton ------------------------------ */

export function Skeleton({ lines = 3, className }: { lines?: number; className?: string }) {
  return (
    <div className={cx("pl-skeleton", className)} aria-hidden="true" data-testid="skeleton">
      {Array.from({ length: lines }).map((_, index) => (
        <span
          key={index}
          className={cx("pl-skeleton__line", `pl-skeleton__line--${(index % 3) + 1}`)}
        />
      ))}
    </div>
  );
}

/* ---------------------------- Empty/Error ---------------------------- */

export interface StateProps {
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  icon?: ReactNode;
  tone?: "empty" | "error";
}

export function StateBlock({ title, description, action, icon, tone = "empty" }: StateProps) {
  return (
    <div className={cx("pl-state", `pl-state--${tone}`)} role={tone === "error" ? "alert" : undefined}>
      <div className="pl-state__icon" aria-hidden="true">
        {icon ?? <MoleculeGlyph size={40} />}
      </div>
      <h3 className="pl-state__title">{title}</h3>
      {description ? <p className="pl-state__desc">{description}</p> : null}
      {action ? <div className="pl-state__action">{action}</div> : null}
    </div>
  );
}

/* ------------------------------- 品牌图形 ---------------------------- */

/** 分子环：把「知识节点 + 连线」作为品牌图形，避免通用后台观感。 */
export function MoleculeGlyph({ size = 32, className }: { size?: number; className?: string }) {
  return (
    <svg
      className={cx("pl-molecule", className)}
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="24" cy="9" r="4.4" className="pl-molecule__node" />
      <circle cx="39" cy="19" r="3.6" className="pl-molecule__node pl-molecule__node--soft" />
      <circle cx="33" cy="38" r="4" className="pl-molecule__node" />
      <circle cx="13" cy="35" r="3.4" className="pl-molecule__node pl-molecule__node--soft" />
      <circle cx="9" cy="17" r="4.2" className="pl-molecule__node" />
      <circle cx="24" cy="24" r="2.6" className="pl-molecule__core" />
      <path
        d="M24 9 39 19 33 38 13 35 9 17Z"
        className="pl-molecule__bond"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M24 9V24M39 19 24 24M33 38 24 24M13 35 24 24M9 17 24 24" className="pl-molecule__bond-thin" strokeWidth="1.1" />
    </svg>
  );
}

/** 胶囊形进度指示：把「服药/剂量」的意象用于学习进度。 */
export function CapsuleProgress({ value, max, label }: { value: number; max: number; label: string }) {
  return (
    <progress
      className="pl-capsule"
      value={Math.max(0, Math.min(value, max))}
      max={max > 0 ? max : 1}
      aria-label={`${label} ${value}/${max}`}
    />
  );
}
