/**
 * API 数据类型 —— 手工对齐后端 OpenAPI（契约只读，禁止在此"发明"字段）。
 * 来源：`backend/app/schemas/*.py`，字段名与可空性严格一致。
 */

export type ISODateString = string;

export interface User {
  id: number;
  username: string;
  created_at: ISODateString | null;
}

export interface TokenResponse {
  access_token: string;
  token_type?: string;
  expires_in: number;
  user: User;
}

/** Cookie 会话登录的响应：只有用户信息，不含 token。 */
export interface SessionResponse {
  user: User;
}

export interface LoginRequest {
  username: string;
  password: string;
}

export interface RegisterRequest {
  username: string;
  password: string;
}

/** 三维度标签：科目 / 章节 / 考点类型 */
export interface CardTags {
  subjects?: string[];
  chapters?: string[];
  point_types?: string[];
}

export interface Card {
  id: number;
  user_id: number;
  front: string;
  back: string;
  tags: CardTags & Record<string, unknown>;
  created_at: ISODateString | null;
  fsrs_stability: number | null;
  fsrs_difficulty: number | null;
  fsrs_due: ISODateString | null;
  fsrs_last_review: ISODateString | null;
  fsrs_state: number | null;
  fsrs_step: number | null;
  is_new: boolean;
  is_due: boolean;
}

export interface DueCardsResponse {
  total_due: number;
  offset: number;
  limit: number;
  weak_card_ids: number[];
  weak_point_count: number;
  items: Card[];
}

export interface PreviewOption {
  rating: number;
  label: string;
  interval_days: number;
  interval_label: string;
  due: ISODateString | null;
}

export interface ReviewPreview {
  card_id: number;
  is_new: boolean;
  retrievability: number | null;
  options: PreviewOption[];
}

/** 1=忘了 2=困难 3=一般 4=简单 */
export type Rating = 1 | 2 | 3 | 4;

export interface GradeRequest {
  rating: Rating;
  reviewed_at?: ISODateString | null;
  elapsed_days?: number | null;
}

export interface GradeResponse {
  card: Card;
  rating: number;
  rating_label: string;
  elapsed_days: number;
  interval_days: number;
  interval_label: string;
  due: ISODateString | null;
  retrievability: number | null;
}

export interface ReviewLog {
  id: number;
  card_id: number;
  rating: number;
  reviewed_at: ISODateString | null;
  elapsed_days: number;
  rating_label: string;
}
