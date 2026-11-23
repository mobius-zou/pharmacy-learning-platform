# 认证改造：独立登录/注册页 + Cookie 会话 + next 回跳

> 需求：`/login` 独立路由、`?next=` 守卫与回跳、Bearer→HttpOnly Cookie、
> 登出回登录页、注册/忘记密码独立页面。后端已按授权修改（仅认证相关）。

## 一、后端改动（唯一被改动的后端部分）

| 文件 | 改动 |
| --- | --- |
| `app/config.py` | 新增 `AUTH_COOKIE_NAME/SECURE/SAMESITE/PATH` |
| `app/api/v1/auth.py` | 新增 `POST /auth/session`（只写 Cookie，响应体无 token）；`POST /auth/login` 顺手写 Cookie；`POST /auth/logout` 清除 Cookie；`register` 在响应前显式 `commit` |
| `app/deps.py` | 取令牌改为「Authorization 头优先，缺失时回退 Cookie」 |
| `app/schemas/auth.py` | 新增 `SessionResponse`（只有 `user`） |
| `tests/test_auth_cookie.py` | 新增 10 项回归 |
| `scripts/probe_register_race.py` | 注册→立即登录的竞态探测脚本 |
| `.env.example`、`README.md` | 文档 |

**为什么新增 `/auth/session` 而不是改 `/auth/login` 的响应**：`/login` 返回
`access_token` 是既有单页与脚本的契约，删字段会破坏它们；新增端点让正式前端
拿到「响应体没有 token」的形态，两条通道互不影响。

## 二、前端改动

| 文件 | 改动 |
| --- | --- |
| `src/shared/api/client.ts` | 改为 `credentials: "include"`，不再读写任何 token；401 广播事件并清理旧单页遗留的 `pharm_token` |
| `src/features/auth/auth-provider.tsx` | 无 token 状态；`user + checking`；登录走 `/auth/session`；登出走 `/auth/logout`；首屏用 `/auth/me` 恢复会话 |
| `src/features/auth/login-page.tsx` | 独立登录页，读 `?next=` |
| `src/features/auth/register-page.tsx` | **新增**独立注册页（含确认密码与前端校验），读 `?next=` |
| `src/shared/lib/next.ts` | `safeNextPath()` / `loginPathFor()`：防开放重定向 |
| `src/app/routes.tsx` | 守卫改为重定向 `/login?next=…`；新增 `/register` |
| `src/shared/ui/primitives.tsx` | 进度条改原生 `<progress>`，去掉内联 style |
| `vite.config.ts` | 构建期注入严格 CSP（开发期不注入，否则 Vite 内联样式被拦） |
| `src/shared/styles/*.css` | 原生 progress 样式；窄屏开始面板改纵向堆叠（修 390px 溢出） |

## 三、验证

```
后端：750 passed（含 10 项 Cookie 回归）· alembic check 无漂移 · ruff 通过
前端：typecheck 通过 · 70 单测通过 · build 通过（gzip JS 79.6KB / CSS 5.7KB）
E2E：8 passed × 3 轮（桌面 + 移动，含 next 回跳与开放重定向防护）
溢出：390 / 375 / 320px × 3 页面全部无横向滚动
```

## 四、过程中发现并修复的 2 个真实缺陷

### 1. 「注册成功后立即登录」偶发 401（后端竞态）

`get_db` 的 `commit` 发生在**响应发出之后**，注册响应先到浏览器，
紧随其后的登录请求可能查不到刚插入的用户。

实测（修复前）：16 组并发「注册→立即登录」有 **7 组** 返回 `(201, 401)`；
修复后：24 组全部 `(201, 200)`。复现脚本：`backend/scripts/probe_register_race.py`。

修法：注册路径在返回前显式 `db.commit()`，保证读己之写。

### 2. 严格 CSP 在开发期把页面打成无样式

CSP 写在 `index.html` 里时，Vite 开发期用内联 `<style>` 注入 CSS，
`style-src 'self'` 把它们全部拦掉（截图里页面完全无样式）。
修法：CSP 改由 `vite.config.ts` 的 `pharm-inject-csp` 插件**只在 build 注入**，
并有单测锁定「index.html 里不能出现 CSP」。

顺带修掉一个被无样式掩盖的移动端溢出：窄屏下胶囊进度占满一行但父级仍是横向
flex，把标题挤成 23px 宽并溢出到 411px。

## 五、安全说明与遗留

- **CSRF**：Cookie 为 `SameSite=Lax` + 接口只收 `application/json`，
  常规跨站表单提交不成立；若未来需要跨站嵌入，应加双重提交 token。
- **生产必做**：HTTPS + `AUTH_COOKIE_SECURE=true`；建议把 `JWT_EXPIRE_MINUTES`
  调小（如 60–120）以缩短会话窗口。
- **忘记密码**：后端没有对应接口（已确认无 `password/reset/forgot` 端点），
  因此**没有**做页面 —— 不虚构接口。需要时先加
  `POST /auth/password/reset-request` + 邮件令牌流程。
- **旧的 `/ui/literature` 单页**：未改动。它用 Bearer + localStorage，
  同源请求也会自动带上 Cookie，因此在新会话下同样可用。
