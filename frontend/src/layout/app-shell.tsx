import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "@/features/auth/auth-provider";
import { Button, MoleculeGlyph, cx } from "@/shared/ui/primitives";
import { ThemeToggle } from "@/shared/ui/theme-toggle";
import { useToast } from "@/shared/ui/toast";

interface NavItem {
  to: string;
  label: string;
  /** 窄屏（≤900px）底部导航用的短标签，避免 6 项放不下被截断 */
  shortLabel: string;
  hint: string;
  ready: boolean;
}

/** 业务入口；本轮先交付「复习 / 卡片 / 文献」，其余为可见的后续入口。 */
const NAV: NavItem[] = [
  { to: "/review", label: "今日复习", shortLabel: "复习", hint: "到期卡片 · 四档评分", ready: true },
  { to: "/wrong-questions", label: "错题诊断", shortLabel: "错题", hint: "知识点识别 · 薄弱点", ready: false },
  { to: "/experiments", label: "实验档案", shortLabel: "实验", hint: "偏差回溯 · 模板", ready: false },
  { to: "/exam-points", label: "考点匹配", shortLabel: "考点", hint: "试匹配 · 优先级", ready: false },
  { to: "/cards", label: "卡片管理", shortLabel: "卡片", hint: "手动录入 · 批量", ready: true },
  { to: "/literature", label: "文献工作台", shortLabel: "文献", hint: "人工校对后入库", ready: true },
];

export function AppShell() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();

  async function onLogout() {
    // 服务端吊销令牌 + 清 Cookie，本地内存态一并清空，再回登录页
    await logout();
    toast.push("已登出", "info");
    navigate("/login", { replace: true });
  }

  return (
    <div className="pl-shell">
      <aside className="pl-nav" aria-label="主导航">
        <div className="pl-nav__brand">
          <MoleculeGlyph size={34} />
          <div className="pl-nav__brand-text">
            <span className="pl-nav__brand-name">药学学习</span>
            <span className="pl-nav__brand-sub">学习闭环</span>
          </div>
        </div>

        <nav className="pl-nav__list">
          {NAV.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) => cx("pl-nav__item", isActive && "is-active")}
              aria-label={`${item.label}：${item.hint}`}
            >
              <span className="pl-nav__dot" aria-hidden="true" />
              <span className="pl-nav__text">
                <span className="pl-nav__label pl-nav__label--full">{item.label}</span>
                <span className="pl-nav__label pl-nav__label--short">{item.shortLabel}</span>
                <span className="pl-nav__hint">{item.hint}</span>
              </span>
              {!item.ready ? (
                <span className="pl-nav__soon" aria-hidden="true">
                  即将
                </span>
              ) : null}
            </NavLink>
          ))}
        </nav>

        <div className="pl-nav__foot">
          <ThemeToggle />
        </div>
      </aside>

      <div className="pl-main">
        <header className="pl-topbar">
          <div className="pl-topbar__user">
            <span className="pl-avatar" aria-hidden="true">
              {(user?.username ?? "?").slice(0, 1).toUpperCase()}
            </span>
            <div className="pl-topbar__meta">
              <span className="pl-topbar__name">{user?.username ?? "未登录"}</span>
              <span className="pl-topbar__sub">保持每天一点点</span>
            </div>
          </div>
          <div className="pl-topbar__actions">
            <ThemeToggle className="pl-topbar__theme" />
            <Button variant="ghost" size="sm" onClick={() => void onLogout()}>
              登出
            </Button>
          </div>
        </header>

        <main className="pl-content" id="main">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
