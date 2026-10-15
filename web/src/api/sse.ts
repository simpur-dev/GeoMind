/**
 * SSE client（架构 §9.3 事件流）。
 *
 * 断线续传：浏览器 EventSource 重连时自动携带 Last-Event-ID header，
 * 后端（backend/api/routes.py events 端点）按 seq 重放，不漏不重。
 */
import type { SseEvent } from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

export type EventHandler = (ev: SseEvent) => void;

export class SseClient {
  private es: EventSource | null = null;
  private handlers = new Map<string, Set<EventHandler>>();
  private wildcard = new Set<EventHandler>();

  constructor(private readonly sessionId: string) {}

  on(type: string, fn: EventHandler): () => void {
    if (type === "*") {
      this.wildcard.add(fn);
      return () => this.wildcard.delete(fn);
    }
    const set = this.handlers.get(type) ?? new Set();
    set.add(fn);
    this.handlers.set(type, set);
    return () => set.delete(fn);
  }

  start(): void {
    if (this.es) return;
    this.es = new EventSource(`${BASE}/api/sessions/${this.sessionId}/events`);
    this.es.onopen = () => {
      this.dispatch({ type: "sse.open", data: {} });
    };
    this.es.onmessage = (raw) => {
      let ev: SseEvent;
      try {
        ev = JSON.parse(raw.data) as SseEvent;
      } catch {
        return;
      }
      this.dispatch(ev);
    };
    this.es.onerror = () => {
      // EventSource 自动重连（带 Last-Event-ID）；仅透出可观测信号
      this.dispatch({ type: "sse.reconnecting", data: {} });
    };
  }

  stop(): void {
    this.es?.close();
    this.es = null;
  }

  private dispatch(ev: SseEvent): void {
    for (const fn of this.handlers.get(ev.type) ?? []) fn(ev);
    for (const fn of this.wildcard) fn(ev);
  }
}
