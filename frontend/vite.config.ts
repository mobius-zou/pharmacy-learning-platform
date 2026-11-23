import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// 开发期把 /api 代理到后端，避免跨域与 CORS 依赖；
// 生产由反向代理（Nginx/Caddy）承担同一职责，见 README。
/**
 * 只在**生产构建**注入严格 CSP。
 *
 * 为什么不能写在 index.html 里：Vite 开发期通过内联 <style> 注入样式
 * （HMR），`style-src 'self'` 会把它们全部拦掉 —— 页面变成无样式。
 * 构建产物里 CSS 是外链文件，严格 CSP 才能成立。
 *
 * 生产部署建议由反代下发同名响应头（meta 里的 CSP 不支持 frame-ancestors）。
 */
const CSP_POLICY = [
  "default-src 'self'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'self'",
].join("; ");

function injectCsp() {
  return {
    name: "pharm-inject-csp",
    apply: "build" as const,
    transformIndexHtml(html: string) {
      const meta = `    <meta http-equiv="Content-Security-Policy" content="${CSP_POLICY}" />\n`;
      return html.replace("</head>", `${meta}  </head>`);
    },
  };
}

// 开发代理目标可用 VITE_API_PROXY 覆盖（默认本机 8000）。
// 便于同时跑多个后端版本做对比，或后端换了端口。
const apiProxyTarget = process.env.VITE_API_PROXY ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), injectCsp()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: apiProxyTarget, changeOrigin: true },
    },
  },
  build: { outDir: "dist", sourcemap: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    css: false,
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
