import { useState } from "react";
import { Link, Navigate, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError } from "@/shared/api/errors";
import { safeNextPath } from "@/shared/lib/next";
import { Button, Field, Input, MoleculeGlyph, Panel } from "@/shared/ui/primitives";
import { useToast } from "@/shared/ui/toast";
import { useAuth } from "./auth-provider";

interface FieldErrors {
  username?: string;
  password?: string;
  confirm?: string;
}

/** 前端先做可读校验，避免把「密码太短」这类问题留给 422 的英文提示。 */
function validate(username: string, password: string, confirm: string): FieldErrors {
  const errors: FieldErrors = {};
  const name = username.trim();
  if (name.length < 3) errors.username = "用户名至少 3 个字符";
  else if (name.length > 64) errors.username = "用户名最多 64 个字符";
  else if (/\s/.test(name)) errors.username = "用户名不能包含空白字符";
  if (password.length < 6) errors.password = "密码至少 6 个字符";
  else if (password.length > 128) errors.password = "密码最多 128 个字符";
  if (confirm !== password) errors.confirm = "两次输入的密码不一致";
  return errors;
}

export function RegisterPage() {
  const { user, register, login, checking } = useAuth();
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const toast = useToast();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const next = safeNextPath(params.get("next"));
  if (user && !checking) return <Navigate to={next} replace />;

  async function onSubmit(event: React.FormEvent) {
    event.preventDefault();
    setFormError(null);
    const nextErrors = validate(username, password, confirm);
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    setSubmitting(true);
    try {
      await register({ username: username.trim(), password });
      await login({ username: username.trim(), password });
      toast.push("注册成功，开始你的第一轮复习吧", "ok");
      navigate(next, { replace: true });
    } catch (error) {
      setFormError(error instanceof ApiError ? error.message : "注册失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="pl-auth">
      <section className="pl-auth__intro" aria-labelledby="pl-register-title">
        <div className="pl-auth__brand">
          <MoleculeGlyph size={44} />
          <span>药学学习平台</span>
        </div>
        <h1 id="pl-register-title" className="pl-auth__headline">
          从今天的一张卡
          <br />
          <span className="pl-auth__headline-accent">开始积累</span>
        </h1>
        <p className="pl-auth__lede">
          账号只用于保存你的卡片、错题与学习记录，不会用于排名或公开比较。
        </p>
        <ul className="pl-auth__points">
          <li>密码至少 6 位，建议字母与数字混合</li>
          <li>登录状态保存在 HttpOnly Cookie，脚本读不到令牌</li>
          <li>随时可以登出，登出会吊销该账号的全部令牌</li>
        </ul>
      </section>

      <Panel as="section" className="pl-auth__card" aria-labelledby="pl-register-form-title">
        <h2 id="pl-register-form-title" className="pl-auth__form-title">
          创建学习账号
        </h2>

        <form className="pl-auth__form" onSubmit={onSubmit} noValidate>
          <Field label="用户名" htmlFor="username" error={errors.username} required>
            <Input
              id="username"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              placeholder="3–64 个字符"
              aria-invalid={Boolean(errors.username)}
              aria-required="true"
              autoFocus
            />
          </Field>

          <Field
            label="密码"
            htmlFor="password"
            error={errors.password}
            hint="至少 6 个字符"
            required
          >
            <Input
              id="password"
              name="password"
              type="password"
              autoComplete="new-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              aria-invalid={Boolean(errors.password)}
              aria-required="true"
            />
          </Field>

          <Field label="确认密码" htmlFor="confirm" error={errors.confirm} required>
            <Input
              id="confirm"
              name="confirm"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              aria-invalid={Boolean(errors.confirm)}
              aria-required="true"
            />
          </Field>

          {formError ? (
            <p className="pl-form-error" role="alert">
              {formError}
            </p>
          ) : null}

          <Button type="submit" size="lg" block loading={submitting}>
            注册并登录
          </Button>
        </form>

        <p className="pl-auth__footnote">
          已经有账号？
          <Link to={next === "/review" ? "/login" : `/login?next=${encodeURIComponent(next)}`}>
            去登录
          </Link>
        </p>
      </Panel>
    </main>
  );
}
