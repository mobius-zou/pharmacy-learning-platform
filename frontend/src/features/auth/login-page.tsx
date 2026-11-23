import { useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import { ApiError } from "@/shared/api/errors";
import { safeNextPath } from "@/shared/lib/next";
import { Button, Field, Input, MoleculeGlyph, Panel } from "@/shared/ui/primitives";
import { useToast } from "@/shared/ui/toast";
import { useAuth } from "./auth-provider";

export function LoginPage() {
  const { user, login, checking } = useAuth();
  const [params] = useSearchParams();
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<{ username?: string; password?: string }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const next = safeNextPath(params.get("next"));

  // 已登录：直接去原本要去的页面（或首页）
  if (user && !checking) return <Navigate to={next} replace />;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setFormError(null);

    const nextErrors: { username?: string; password?: string } = {};
    if (!username.trim()) nextErrors.username = "请输入用户名";
    if (!password) nextErrors.password = "请输入密码";
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    setSubmitting(true);
    try {
      await login({ username: username.trim(), password });
      toast.push("欢迎回来，开始今天的学习吧", "ok");
      // 跳转由上面的 <Navigate> 完成（user 变化后重新渲染）
    } catch (error) {
      setFormError(error instanceof ApiError ? error.message : "登录失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="pl-auth">
      <section className="pl-auth__intro" aria-labelledby="pl-auth-title">
        <div className="pl-auth__brand">
          <MoleculeGlyph size={44} />
          <span>药学学习平台</span>
        </div>
        <h1 id="pl-auth-title" className="pl-auth__headline">
          把「记不住」变成
          <br />
          <span className="pl-auth__headline-accent">看得见的进步</span>
        </h1>
        <p className="pl-auth__lede">
          错题 → 薄弱点 → 复习调度 → 文献知识 → 实验复盘 → 考点对齐，
          一个闭环走完你的药学学习。
        </p>
        <ul className="pl-auth__points">
          <li>到期卡片按薄弱点优先排队，打开就能开始</li>
          <li>每次评分都能看到下次复习间隔，心里有数</li>
          <li>每 10 张一次轻量休息提示，不打断节奏</li>
        </ul>
      </section>

      <Panel as="section" className="pl-auth__card" aria-labelledby="pl-auth-form-title">
        <h2 id="pl-auth-form-title" className="pl-auth__form-title">
          登录
        </h2>
        <p className="pl-muted pl-auth__form-lede">
          登录状态保存在 HttpOnly Cookie 中，页面脚本读不到你的令牌。
        </p>

        <form className="pl-auth__form" onSubmit={onSubmit} noValidate>
          <Field label="用户名" htmlFor="username" error={errors.username} required>
            <Input
              id="username"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="你的用户名"
              aria-invalid={Boolean(errors.username)}
              aria-required="true"
              autoFocus
            />
          </Field>

          <Field label="密码" htmlFor="password" error={errors.password} required>
            <Input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              aria-invalid={Boolean(errors.password)}
              aria-required="true"
            />
          </Field>

          {formError ? (
            <p className="pl-form-error" role="alert">
              {formError}
            </p>
          ) : null}

          <Button type="submit" size="lg" block loading={submitting}>
            登录
          </Button>
        </form>

        <p className="pl-auth__footnote">
          还没有账号？
          <Link to={next === "/review" ? "/register" : `/register?next=${encodeURIComponent(next)}`}>
            注册一个
          </Link>
          <span className="pl-auth__sep" aria-hidden="true">
            ·
          </span>
          <Link to="/literature">先看看文献工作台</Link>
        </p>
      </Panel>
    </main>
  );
}
