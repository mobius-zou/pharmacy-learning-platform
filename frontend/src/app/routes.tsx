import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { ReactNode } from "react";
import { LoginPage } from "@/features/auth/login-page";
import { RegisterPage } from "@/features/auth/register-page";
import { useAuth } from "@/features/auth/auth-provider";
import { loginPathFor } from "@/shared/lib/next";
import { ReviewPage } from "@/features/review/review-page";
import { AppShell } from "@/layout/app-shell";
import { CardsPage, ComingSoonPage, LiteraturePage, NotFoundPage } from "@/pages/placeholders";
import { Skeleton } from "@/shared/ui/primitives";

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, checking } = useAuth();
  const location = useLocation();

  if (checking) {
    return (
      <div className="pl-content pl-content--center">
        <Skeleton lines={3} />
      </div>
    );
  }
  // 未登录：把原本要去的地址带上，登录后原路返回
  if (!user) {
    return <Navigate to={loginPathFor(location.pathname, location.search)} replace />;
  }
  return <>{children}</>;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<Navigate to="/review" replace />} />
        <Route path="/review" element={<ReviewPage />} />
        <Route path="/cards" element={<CardsPage />} />
        <Route path="/literature" element={<LiteraturePage />} />
        <Route
          path="/wrong-questions"
          element={
            <ComingSoonPage
              title="错题诊断"
              description="错题录入、知识点识别（需人工确认）与薄弱点警报。后端接口已就绪，界面在下一次交付。"
            />
          }
        />
        <Route
          path="/experiments"
          element={
            <ComingSoonPage
              title="实验档案"
              description="实验模板、偏差回溯与历史偏差提示。后端接口已就绪，界面在下一次交付。"
            />
          }
        />
        <Route
          path="/exam-points"
          element={
            <ComingSoonPage
              title="考点匹配"
              description="试匹配、导入、三级优先级清单与高危入队。后端接口已就绪，界面在下一次交付。"
            />
          }
        />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
