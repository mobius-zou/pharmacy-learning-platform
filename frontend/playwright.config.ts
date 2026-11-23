import { defineConfig, devices } from "@playwright/test";

// E2E 需要本机后端在 8000 端口：
//   cd backend && ./venv/bin/uvicorn app.main:app --port 8000
// 首次准备演示数据：node scripts/seed-demo.mjs
// 然后运行：npm run e2e
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    // 用 Pixel 5（Chromium 内核）而非 iPhone（默认 WebKit），
    // 本项目只安装 chromium，避免额外下载 WebKit。
    { name: "mobile", use: { ...devices["Pixel 5"] } },
  ],
  webServer: {
    // 用 dev server：/api 由 vite 代理转发到本机后端（8000），
    // 既避免 CORS 白名单依赖，也不需要在构建期注入绝对地址。
    command: "npm run dev -- --port 5173 --strictPort",
    url: "http://127.0.0.1:5173",
    reuseExistingServer: true,
    timeout: 90_000,
  },
});
