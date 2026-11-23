import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { UNAUTHORIZED_EVENT, api } from "@/shared/api/client";
import type { LoginRequest, RegisterRequest, SessionResponse, User } from "@/shared/api/types";

/**
 * 认证状态。
 *
 * **没有 token 字段**：令牌在 HttpOnly Cookie 里，JS 读不到。
 * 判断是否登录只看「服务端是否认这个 Cookie」—— 即 `/auth/me` 的返回。
 */
interface AuthState {
  user: User | null;
  /** 正在用 /auth/me 校验 Cookie 会话（首屏骨架用） */
  checking: boolean;
  login: (payload: LoginRequest) => Promise<void>;
  register: (payload: RegisterRequest) => Promise<void>;
  logout: () => Promise<void>;
  /** 401 等场景下清空内存态 */
  clear: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);
  const client = useQueryClient();

  const clear = useCallback(() => {
    setUser(null);
    setChecking(false);
    client.clear();
  }, [client]);

  // 首屏恢复会话：Cookie 有效则拿到用户，无效则 401（由 client 广播）
  useEffect(() => {
    let cancelled = false;
    api
      .get<User>("/auth/me")
      .then((me) => {
        if (!cancelled) setUser(me);
      })
      .catch(() => {
        if (!cancelled) setUser(null);
      })
      .finally(() => {
        if (!cancelled) setChecking(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onUnauthorized = () => clear();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [clear]);

  const login = useCallback(async (payload: LoginRequest) => {
    // /auth/session 只在 Cookie 里放令牌，响应体没有 token
    const data = await api.post<SessionResponse>("/auth/session", payload);
    setUser(data.user);
    setChecking(false);
  }, []);

  const register = useCallback(async (payload: RegisterRequest) => {
    await api.post<User>("/auth/register", payload);
  }, []);

  const logout = useCallback(async () => {
    try {
      // 服务端吊销该用户全部令牌并清除 Cookie
      await api.post<void>("/auth/logout");
    } catch {
      /* 忽略：本地登出必须成功 */
    } finally {
      clear();
    }
  }, [clear]);

  const value = useMemo<AuthState>(
    () => ({ user, checking, login, register, logout, clear }),
    [user, checking, login, register, logout, clear],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return context;
}
