/**
 * REST client：与后端 backend/api/routes.py 的契约一一对应。
 *
 * 通道平等观（架构 §9.1）：Web 只是挂在同一套内核契约上的平级通道之一，
 * 这里只做 HTTP，不含任何业务判断。
 */
import type {
  Candidate,
  FeatureCollection,
  GridAgg,
  MapSpec,
  SseEvent,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => "");
    throw new Error(`${init?.method ?? "GET"} ${path} → ${r.status} ${detail}`);
  }
  return (await r.json()) as T;
}

export interface ChatResult {
  session_id: string;
  interrupted: boolean;
  question: Record<string, unknown> | null;
  candidates: Candidate[];
  aggs: GridAgg[];
  decision: Record<string, unknown>;
  map_spec: MapSpec;
}

export const api = {
  createSession: (scene = "site_selection") =>
    req<{ session_id: string; version: number }>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ scene }),
    }),

  getSession: (sid: string) =>
    req<{ session_id: string; state: unknown; graph_state: unknown }>(
      `/api/sessions/${sid}`),

  chat: (sid: string, query: string, gesture?: Record<string, unknown>) =>
    req<ChatResult>(`/api/sessions/${sid}/chat`, {
      method: "POST",
      body: JSON.stringify({ query, gesture }),
    }),

  resume: (sid: string, answer: string) =>
    req<ChatResult>(`/api/sessions/${sid}/resume`, {
      method: "POST",
      body: JSON.stringify({ answer }),
    }),

  /** 手势回传（架构 §9.3 闭环）：几何原样上报（GCJ-02），后端翻译成约束。 */
  feedback: (sid: string, type: string, payload: Record<string, unknown>) =>
    req<{ accepted?: boolean; result?: ChatResult; candidate?: Candidate }>(
      `/api/sessions/${sid}/feedback`, {
        method: "POST",
        body: JSON.stringify({ type, payload }),
      }),

  layer: (sid: string, layerId: string) =>
    req<FeatureCollection>(`/api/sessions/${sid}/layers/${layerId}`),

  skills: () => req<{ skills: unknown[] }>("/api/skills"),
};

export type { SseEvent };
