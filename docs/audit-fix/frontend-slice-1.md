# 前端垂直切片交付说明（认证 + 复习面板）

> 交付范围：`frontend/` 新建 SPA，完成**认证外壳**与**复习面板**两条主线；
> 其余四个业务模块给出可见入口 + 可解释占位。视觉方向请确认后再复制到其余页面。

## 一、改了哪些文件

```
frontend/
  package.json  tsconfig.json  tsconfig.node.json  vite.config.ts  playwright.config.ts  index.html  .env.example
  src/
    main.tsx  vite-env.d.ts
    app/{app,providers,routes}.tsx
    layout/app-shell.tsx
    shared/
      api/{client,errors,types,queries}.ts
      lib/{idempotency,format,theme}.ts
      ui/{primitives,dialog,toast,info-hint,theme-toggle}.tsx
      styles/{tokens,base,components,features}.css
    features/
      auth/{auth-provider.tsx,login-page.tsx}
      review/{session.ts,rating-bar.tsx,review-page.tsx}
    pages/placeholders.tsx
  tests/{setup.ts,api-client.test.ts,session.test.ts,format.test.ts,design-tokens.test.ts}
  e2e/auth-review.spec.ts
  scripts/{seed-demo.mjs,capture-shots.mjs}
  screenshots/*.png（4 组 + 登录/专注）
  README.md
.fonts/fonts.conf            # 仅本机截图用的字体路径（不入库字体）
.gitignore                   # 增加 .npmcache / .playwright / screenshots 产物规则
```

**后端零改动**：`git status --short backend/` 为空（验证记录见下）。

## 二、设计与交互要点（对应硬性要求）

| 要求 | 落地方式 |
| --- | --- |
| 圆润/平滑 | 圆角 12/16px 为主（`--r-md/lg`），卡片 `--r-xl` 22px，阴影大扩散低透明 |
| 间距节奏 | 只使用 4/8/12/16/24/32 六档；单测断言 CSS 里**没有**非 4 倍数的 margin/padding |
| 动效克制 | 160/200/240ms 三档 + 自然缓动；单测断言时长落在 160–240ms |
| 双主题 | 浅/深各一套完整令牌（非反色）；单测断言两套**颜色令牌成对**、无缺项 |
| 进入即学 | 概览页首屏直接给「开始复习」+ 到期数；空状态给下一步 |
| 专注/心流 | 专注模式隐藏导航与顶栏（Esc 退出）；空格翻面、1–4 评分 |
| 疲劳回落 | 每 10 张一次轻量休息页（可跳过/稍后），进度逐张落库，随时暂停不丢 |
| 收束成就 | 成果页：完成量、平均下次间隔、掌握度变化、评分分布 + 温和文案（无排名/无惩罚色） |
| 二次确认 | 评分需确认（原生 `<dialog>`）；不可逆操作才确认，普通操作不打断 |
| 幂等 | 客户端每次提交生成新 UUID 放入 `Idempotency-Key`；E2E 断言该头存在 |
| 术语解释 | `InfoHint` 内联展开（如 FSRS），不堆正文 |
| 品牌独特性 | 分子环图形、胶囊进度条、掌握度四阶色阶（mint→teal→blue→indigo） |

## 三、验证结果

| 项目 | 结果 |
| --- | --- |
| `npx tsc --noEmit` | 通过（src + node 两份配置） |
| `npm test`（Vitest） | **61 passed** |
| `npm run build` | 通过（JS 242KB / gzip 79KB，CSS 26KB / gzip 5.6KB） |
| `npm run e2e` | **4 passed**（桌面 + 移动各 2 条） |
| 375px 横向溢出 | 5 项检查全部通过（桌面/移动 × 浅/深 + 登录页） |
| 对比度 | 18 组文字/背景组合全部 ≥ 4.5:1（机器计算，见 `design-tokens.test.ts`） |
| 核心任务点击数 | 登录 → 开始 → 翻面 → 选评分 → 确认 = **5 次点击**（含登录；已登录状态为 4 次） |
| 后端 diff | 空 |

### 单测覆盖

- `api-client`：Authorization 注入、幂等键（写请求带/读请求不带/每次不同/可显式指定）、
  401 清 token + 广播事件、后端 `message` 透出、422 detail 拼接、413/429/5xx 兜底文案、
  网络错误归一化、204 处理。
- `session`：概览→专注、翻面、评分推进、块边界（10 张）进入休息、跳过休息、
  最后一张直接成果页、暂停保留进度、restart 回概览、成果统计（含新卡不参与掌握度变化）。
- `design-tokens`：间距/圆角/动效/主题成对性/对比度/品牌元素/窄屏规则。

### E2E 覆盖

1. 注册 → 登录 → 空状态 →（API 造卡）→ 开始复习 → 翻面 → 选「一般」→
   确认框 → 断言 `Idempotency-Key` 为 UUID → 成果页 → 登出后 token 清空；
2. 错误密码 → 展示后端 message「用户名或密码错误」→ 停留登录页。

## 四、视觉与可用性自查

**优点**
- 品牌识别：分子环 + 胶囊进度 + 四阶掌握度色阶，不像通用后台模板；
- 复习主任务占据视觉中心，评分按钮同时给出「下次间隔」，决策成本低；
- 深色不是反色：深色主色更亮、阴影更深、面板层级独立设计；
- 移动端 6 个入口用短标签（复习/错题/实验/考点/卡片/文献），390px 不折行。

**已知不足（下一轮改进）**
1. 专注模式目前只隐藏导航/顶栏，未做「卡片更大 + 背景压暗」的进一步沉浸；
2. 评分按钮的四种底色在浅色下偏淡，「一般/简单」对比需再加强（文字已达标，底色区分度可提升）;
3. 队列预览只展示前 8 张，没有「展开全部」；
4. 休息页是静态提示，没有 5 秒倒计时动画（需求允许「可跳过」，当前以按钮替代）；
5. 首次使用没有引导（只靠术语 hint），后续可加一次性欢迎条。

## 五、需要你决策的遗留问题

1. **视觉方向是否确认**：确认后我按同一套令牌补齐错题/实验/考点/卡片四个模块。
2. **连续学习天数**：后端无此指标。是否新增 `GET /review/stats`？
   在接口就绪前，成果页**不展示**该数字（不编造）。
3. **是否要把文献校对页也迁进 SPA**：当前保留原静态页并用外部链接接入
   （满足「不要删除」），若要内嵌需处理其严格 CSP 与 iframe 交互。
4. **生产部署形态**：推荐 Nginx/Caddy 同源反代 `/` → 前端、`/api` + `/ui` + `/docs` → 后端；
   若必须跨域直连，需要你把前端域名加入 `CORS_ORIGINS`（后端改动，需你授权）。
