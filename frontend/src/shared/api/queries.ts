/**
 * 服务端状态统一交给 TanStack Query。
 * 约定：所有请求都通过这里的 hook 发起，页面不写裸 useEffect + fetch。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import type {
  Card,
  DueCardsResponse,
  GradeRequest,
  GradeResponse,
  ReviewLog,
  ReviewPreview,
  User,
} from "./types";

export const queryKeys = {
  me: ["auth", "me"] as const,
  due: (limit: number) => ["review", "due", limit] as const,
  preview: (cardId: number) => ["review", "preview", cardId] as const,
  logs: (cardId: number) => ["review", "logs", cardId] as const,
  card: (cardId: number) => ["cards", cardId] as const,
};

export function useMe(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.me,
    queryFn: () => api.get<User>("/auth/me"),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

export function useDueCards(limit: number) {
  return useQuery({
    queryKey: queryKeys.due(limit),
    queryFn: () => api.get<DueCardsResponse>(`/review/due?limit=${limit}`),
    staleTime: 0,
  });
}

export function usePreview(cardId: number | null) {
  return useQuery({
    queryKey: queryKeys.preview(cardId ?? -1),
    queryFn: () => api.get<ReviewPreview>(`/review/${cardId}/preview`),
    enabled: cardId !== null,
    staleTime: 30_000,
  });
}

export function useCardLogs(cardId: number | null) {
  return useQuery({
    queryKey: queryKeys.logs(cardId ?? -1),
    queryFn: () => api.get<ReviewLog[]>(`/review/${cardId}/logs`),
    enabled: cardId !== null,
    staleTime: 10_000,
  });
}

export interface GradeVariables {
  cardId: number;
  payload: GradeRequest;
  /** 每次提交生成的新 UUID（见 lib/idempotency.ts） */
  idempotencyKey: string;
}

/**
 * 评分：不可重复推进 —— 服务端按 Idempotency-Key 幂等，前端不再自行去重。
 * 成功后刷新到期队列与卡片缓存。
 */
export function useGradeCard() {
  const client = useQueryClient();
  return useMutation<GradeResponse, unknown, GradeVariables>({
    mutationFn: ({ cardId, payload, idempotencyKey }) =>
      api.post<GradeResponse>(`/review/${cardId}/grade`, payload, { idempotencyKey }),
    onSuccess: (_data, variables) => {
      void client.invalidateQueries({ queryKey: ["review", "due"] });
      void client.invalidateQueries({ queryKey: queryKeys.card(variables.cardId) });
      void client.invalidateQueries({ queryKey: queryKeys.logs(variables.cardId) });
    },
  });
}

export function useCard(cardId: number | null) {
  return useQuery({
    queryKey: queryKeys.card(cardId ?? -1),
    queryFn: () => api.get<Card>(`/cards/${cardId}`),
    enabled: cardId !== null,
    staleTime: 30_000,
  });
}
