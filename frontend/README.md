# 药学学习平台 · 前端 SPA

Vite + React + TypeScript 的零依赖业务前端，覆盖平台的五个学习入口。
**本轮交付垂直切片：认证 + 复习面板**；其余模块已有可见入口与占位说明，
按同一套设计语言在后续交付补齐。

## 快速开始

```bash
# 1) 后端（另一个终端；只支持单进程）
cd ../backend && ./venv/bin/uvicorn app.main:app --port 8000

# 2) 演示数据（可选，供截图/E2E 使用）
node scripts/seed-demo.mjs

# 3) 前端
npm install
npm run dev            # http://127.0.0.1:5173
```

开发期 `/api` 由 Vite 代理到 `http://127.0.0.1:8000`（见 `vite.config.ts`），
因此**不依赖后端 CORS 白名单**；生产由反向代理承担同一职责。

## 脚本

| 命令 | 说明 |
| --- | --- |
| `npm run dev` | 开发服务器（5173，带 /api 代理） |
| `npm run build` | 类型检查 + 生产构建（输出 `dist/`） |
| `npm run typecheck` | `tsc --noEmit`（src 与 node 两份配置） |
| `npm test` | Vitest 单测（61 项） |
| `npm run e2e` | Playwright E2E（桌面 + 移动；需后端已启动，见下） |
| `node scripts/capture-shots.mjs` | 生成 `screenshots/`（4 组 + 登录/注册/专注）并做溢出检查 |
| `node scripts/seed-demo.mjs` | 通过公开 API 造演示账号与卡片 |

## 前端配置项（无需改后端）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE` | `/api/v1` | API 前缀。生产若跨域直连后端，设为绝对地址 |
| `VITE_API_PROXY` | `http://127.0.0.1:8000` | 开发期 Vite 代理目标（便于指向别的后端端口） |
| `pharm_theme`（localStorage） | `system` | 主题偏好：`system` / `light` / `dark` |

### 登录态：HttpOnly Cookie（JS 读不到令牌）

- 登录走 `POST /auth/session`：令牌只写入 **HttpOnly Cookie**，响应体不含 token；
- 前端**不保存任何 token**，刷新后靠 `GET /auth/me` 恢复会话；
- 401 统一清态并跳 `/login?next=<原地址>`；
- 登出调 `POST /auth/logout`（吊销全部令牌 + 清 Cookie）后回 `/login`；
- 旧的 `/ui/literature` 单页仍用 `Authorization: Bearer`，后端两种都接受，
  且它的请求是同源，所以 Cookie 会话下也能直接用。

### 路由与跳转

| 路径 | 说明 |
| --- | --- |
| `/login` | 独立登录页（`?next=` 指定登录后回跳地址） |
| `/register` | 独立注册页（同样支持 `?next=`） |
| 其余业务页 | 未登录会重定向到 `/login?next=…` |

`next` 只接受站内绝对路径：`//evil.com`、`https://…`、`/\evil.com`、
`/login`、`/register` 一律回落到 `/review`（防开放重定向，见 `tests/next.test.ts`）。

> **未实现**：忘记密码（后端没有对应接口）。需要时先加后端端点，再做页面。

## 需要后端配合的事项（**未擅自修改**）

1. **CORS（仅生产直连时需要）**：若生产前端与后端不同源且不用反代，
   需要把前端域名加入 `CORS_ORIGINS`。开发期用 Vite 代理，无需改动。
   实测当前默认白名单已包含 `http://localhost:5173`，
   且预检允许 `idempotency-key` 头。
2. **连续学习天数**：成果页想展示「连续天数」，但后端没有该指标
   （`/review/due` 与 `/review/{id}/logs` 都拿不到跨天统计）。
   本轮**没有编造数据**，改为展示完成量、平均下次间隔、掌握度变化。
   建议后端新增 `GET /review/stats`（或用户统计接口）后再补这块。
3. **卡片批量录入界面**：`POST /cards/bulk` 已就绪，界面下一轮补；
   当前「卡片管理」页给出手动录入/文献转卡片的引导。

## 跑 E2E 的推荐姿势

```bash
# 1) 专门给测试起的后端：关掉语义模型 + 放宽限流（E2E 会反复注册）
cd ../backend
ST_ENABLED=false ST_WARMUP_ON_STARTUP=false \
REGISTER_MAX_PER_WINDOW=500 LOGIN_MAX_ATTEMPTS=50 \
./venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001

# 2) 前端（E2E 会自行拉起 dev server，只需指定代理目标）
cd ../frontend
VITE_API_PROXY=http://127.0.0.1:8001 npm run e2e

# 3) 截图（需 dev server 在跑）
VITE_API_PROXY=http://127.0.0.1:8001 npm run dev &
API_URL=http://127.0.0.1:8001/api/v1 node scripts/capture-shots.mjs
```

- `ST_ENABLED=false`：避免冷启动加载 458MB 模型导致首个请求超时；
- 放宽限流：E2E 每轮会创建新账号，默认 20 次/5 分钟会被反复运行打满。

## 设计系统

- **令牌**：`src/shared/styles/tokens.css`（浅色/深色两套，间距 4/8/12/16/24/32，
  圆角 8/12/16/22，动效 160/200/240ms，缓动 `cubic-bezier(.2,.7,.3,1)`）。
- **品牌语言**：分子环 `MoleculeGlyph`（导航、空状态、休息页、成果页）、
  胶囊进度 `CapsuleProgress`、掌握度四阶色阶 `--mastery-1..4`。
- **组件**：`src/shared/ui/`（Button/Panel/Field/Badge/Progress/Skeleton/
  StateBlock/ConfirmDialog/Toast/InfoHint/ThemeToggle）。
- **校验**：`tests/design-tokens.test.ts` 用机器断言锁住间距节奏、圆角刻度、
  动效时长、两套主题的令牌成对性，以及 18 组文字的对比度 ≥ 4.5:1。

## 键盘与无障碍

- 复习：`空格/Enter` 翻面、`1–4` 评分（会先弹确认框）、`Esc` 暂停/退出专注；
- 所有可交互元素可 Tab 到达，焦点有 `:focus-visible` 描边；
- 图标按钮均有 `aria-label`，对话框用原生 `<dialog>`（自带焦点捕获与 Esc）；
- 尊重 `prefers-reduced-motion`；窄屏（≤900px）侧栏转为底部导航。

## 安全

- 严格 CSP **只在生产构建注入**（`vite.config.ts` 的 `pharm-inject-csp` 插件）：
  开发期 Vite 用内联 `<style>` 注入样式，写死 CSP 会让页面失去样式（实测过）。
  生产部署建议由反代下发同名响应头（meta 不支持 `frame-ancestors`）。
- 组件不使用内联 `style`（进度条改用原生 `<progress>`），这是 `style-src 'self'`
  能成立的前提。
- 评分请求带 `Idempotency-Key`（每次提交新 UUID），服务端按键幂等，
  网络超时重发不会重复推进 FSRS。
- 所有插值渲染走 React（默认转义）；不使用 `dangerouslySetInnerHTML`。

## 本机截图环境的字体说明

WSL 发行版默认没有中文字体，直接截图会把汉字渲染成方框。
仓库根目录的 `.fonts/fonts.conf` 把 Windows 字体目录纳入 fontconfig 搜索路径
（不拷贝字体、不入库），截图脚本会自动设置 `FONTCONFIG_FILE` 使用它。
