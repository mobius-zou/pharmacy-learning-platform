import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/app";
import { Providers } from "./app/providers";
import { applyTheme, readThemePreference, resolveTheme } from "./shared/lib/theme";
import "./shared/styles/tokens.css";
import "./shared/styles/base.css";
import "./shared/styles/components.css";
import "./shared/styles/features.css";

applyTheme(resolveTheme(readThemePreference()));

const container = document.getElementById("root");
if (!container) throw new Error("找不到 #root 挂载点");

createRoot(container).render(
  <StrictMode>
    <Providers>
      <App />
    </Providers>
  </StrictMode>,
);
