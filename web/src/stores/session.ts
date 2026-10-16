/**
 * 会话视图状态（Pinia，架构 §9.5：仅视图缓存，**非权威**）。
 *
 * 权威在后端：Spatial State 在 OB、图执行态在 LangGraph checkpointer。
 * 本 store 只做三件事——把 REST 结果与 SSE 事件映射成可渲染视图、
 * 缓存图层数据（GeoJSON，避免重复拉取）、承载手势回传。
 *
 * SSE 事件 → 视图映射表见 handleEvent（架构 §9.3 事件类型表）。
 */
import { defineStore } from "pinia";

import { api, type ChatResult } from "@/api/client";
import { SseClient } from "@/api/sse";
import type { Candidate, FeatureCollection, MapSpec, SseEvent } from "@/api/types";

// 7 步图节点（与 backend/brain/graph.py GRAPH_NODES 对齐）
export const GRAPH_NODES = [
  "intent", "constraints", "candidates", "spatial_join",
  "decide", "render", "feedback",
] as const;
export type GraphNode = (typeof GRAPH_NODES)[number];

export const NODE_LABELS: Record<GraphNode, string> = {
  intent: "意图", constraints: "约束", candidates: "候选召回",
  spatial_join: "空间连接", decide: "决策", render: "渲染", feedback: "反馈",
};

export type NodeState = "pending" | "running" | "done";

export interface ChatMessage {
  id: number;
  role: "user" | "assistant" | "tool";
  text: string;
}

/** Skill 名 → 图节点（tool.start/end 事件推进进度条）。 */
const SKILL_TO_NODE: Record<string, GraphNode> = {
  multi_modal_search: "candidates",
  spatial_join_agg: "spatial_join",
  reachability: "spatial_join",     // 等时圈与网格聚合同属第 4 步「空间计算」
};

function freshProgress(): Record<GraphNode, NodeState> {
  return Object.fromEntries(GRAPH_NODES.map((n) => [n, "pending"])) as Record<GraphNode, NodeState>;
}

interface SessionState {
  sessionId: string | null;
  /** SSE 连接状态。 */
  connected: boolean;
  /** 一轮对话执行中（chat 请求未返回）。 */
  busy: boolean;
  messages: ChatMessage[];
  /** 最近一次用户输入的原文（draw_polygon 重跑时随手势一起回传给后端）。 */
  lastQuery: string;
  progress: Record<GraphNode, NodeState>;
  /** 最近一次 constraints.updated（左栏 chips 展示）。 */
  constraints: Record<string, unknown> | null;
  /** 权威 Map Spec（后端下发，前端只消费）。 */
  mapSpec: MapSpec | null;
  /** SSE map.view（镜头定位事件）。 */
  viewEvent: { center: [number, number]; zoom: number } | null;
  /** 图层数据缓存（layerId → GeoJSON；仅视图缓存）。 */
  layerData: Record<string, FeatureCollection>;
  /** 图层开关（前端本地视图状态，不回传）。 */
  visibleLayers: Record<string, boolean>;
  /** 列表 ↔ 地图联动的选中候选。 */
  selectedPoiId: number | null;
  /** 点选手势回传查到的候选详情。 */
  pickedCandidate: Candidate | null;
  candidates: Candidate[];
  decision: Record<string, unknown>;
  /** HITL 中断问题（interrupt.required / chat 返回 interrupted）。 */
  question: Record<string, unknown> | null;
  /** 渲染节拍：mapSpec / layerData 变化完成后自增，MapCanvas 据此触发增量渲染。 */
  renderTick: number;
  error: string | null;
}

let msgSeq = 0;
/** SSE 连接不进 reactive state（EventSource 不宜被 proxy 包裹），单会话演示模块级持有。 */
let sseClient: SseClient | null = null;

export const useSessionStore = defineStore("session", {
  state: (): SessionState => ({
    sessionId: null,
    connected: false,
    busy: false,
    messages: [],
    lastQuery: "",
    progress: freshProgress(),
    constraints: null,
    mapSpec: null,
    viewEvent: null,
    layerData: {},
    visibleLayers: {},
    selectedPoiId: null,
    pickedCandidate: null,
    candidates: [],
    decision: {},
    question: null,
    renderTick: 0,
    error: null,
  }),

  actions: {
    // ------------------------------------------------------------------
    // 生命周期
    // ------------------------------------------------------------------
    async init(): Promise<void> {
      this.error = null;
      const { session_id } = await api.createSession();
      this.sessionId = session_id;
      this.pushMsg("assistant", "会话已建立。试试：帮我在春熙路附近选个咖啡馆位置。");
      sseClient = new SseClient(session_id);
      sseClient.on("*", (ev) => this.handleEvent(ev));
      sseClient.start();
    },

    dispose(): void {
      sseClient?.stop();
      sseClient = null;
    },

    // ------------------------------------------------------------------
    // 对话（POST chat 同步返回 + SSE 事件并行推进视图）
    // ------------------------------------------------------------------
    async ask(query: string): Promise<void> {
      if (!this.sessionId || this.busy || !query.trim()) return;
      this.beginRound();
      this.pushMsg("user", query);
      this.lastQuery = query;
      try {
        const r = await api.chat(this.sessionId, query.trim());
        this.applyResult(r);
      } catch (e) {
        this.fail(e);
      }
    },

    async resume(answer: string): Promise<void> {
      if (!this.sessionId || this.busy || !answer.trim()) return;
      this.beginRound();
      this.pushMsg("user", answer);
      this.question = null;
      try {
        const r = await api.resume(this.sessionId, answer.trim());
        this.applyResult(r);
      } catch (e) {
        this.fail(e);
      }
    },

    // ------------------------------------------------------------------
    // 手势回传（架构 §9.3 闭环：手势几何原样上报，后端翻译裁决）
    // ------------------------------------------------------------------
    async feedbackPointSelect(feature: { properties: Record<string, unknown> }): Promise<void> {
      if (!this.sessionId) return;
      const poiId = Number(feature.properties.poi_id);
      try {
        const r = await api.feedback(this.sessionId, "point_select", { poi_id: poiId });
        this.selectedPoiId = poiId;
        this.pickedCandidate = (r.candidate as Candidate | undefined) ?? null;
      } catch (e) {
        this.fail(e);
      }
    },

    /** 框选（draw_polygon）：GCJ-02 WKT 原样上报，后端转 WGS84 重跑一轮。 */
    async feedbackPolygon(polygon: [number, number][]): Promise<void> {
      if (!this.sessionId || this.busy) return;
      const wkt = `POLYGON((${polygon.map(([x, y]) => `${x} ${y}`).join(", ")}))`;
      this.beginRound();
      this.pushMsg("user", "框选了一个区域，按新范围重新分析。");
      try {
        const r = await api.feedback(this.sessionId, "draw_polygon",
          { polygon_wkt: wkt, query: this.lastQuery });
        if (r.result) this.applyResult(r.result as ChatResult);
      } catch (e) {
        this.fail(e);
      }
    },

    // ------------------------------------------------------------------
    // 视图操作
    // ------------------------------------------------------------------
    toggleLayer(id: string): void {
      this.visibleLayers[id] = !(this.visibleLayers[id] ?? true);
      this.renderTick += 1;
    },

    /** 列表 ↔ 地图高亮联动（§9.2 右栏）。 */
    selectPoi(poiId: number | null): void {
      this.selectedPoiId = this.selectedPoiId === poiId ? null : poiId;
      if (this.selectedPoiId === null) this.pickedCandidate = null;
    },

    // ------------------------------------------------------------------
    // 内部
    // ------------------------------------------------------------------
    beginRound(): void {
      this.busy = true;
      this.error = null;
      this.question = null;
      this.progress = freshProgress();
    },

    applyResult(r: ChatResult): void {
      if (r.interrupted) {
        this.question = r.question ?? {};
        this.busy = false;
        this.pushMsg("assistant", `需要补充信息：${(r.question as { question?: string })?.question ?? ""}`);
        return;
      }
      this.candidates = r.candidates;
      this.decision = r.decision;
      this.mapSpec = r.map_spec;
      this.busy = false;
      // message.end（SSE）常先于 HTTP 返回到达，此刻 busy 仍为 true 会错过
      // 把「反馈」置完成的时机，这里兜底收口（与 handleEvent 的 message.end 等价）
      this.progress.feedback = "done";
      for (const l of r.map_spec.layers) this.visibleLayers[l.id] ??= true;
      const summary = String(r.decision.summary ?? "");
      if (summary) this.pushMsg("assistant", summary);
      void this.fetchLayers(r.map_spec).then(() => { this.renderTick += 1; });
    },

    /** 按 source.href 拉取图层 GeoJSON（stale 图层先失效缓存）。 */
    async fetchLayers(spec: MapSpec): Promise<void> {
      if (!this.sessionId) return;
      const next: Record<string, FeatureCollection> = {};
      for (const l of spec.layers) {
        if (l.status === "stale") continue; // stale 先隐藏旧几何，待 ready 再拉
        if (l.source?.type === "geojson" && l.source.href) {
          try {
            next[l.id] = await api.layer(this.sessionId, l.id);
          } catch {
            console.warn(`[session] 图层 ${l.id} 数据拉取失败`);
          }
        }
      }
      this.layerData = next;
    },

    /**
     * SSE 事件 → 视图映射（架构 §9.3 事件类型表）。
     * 进度条按 7 步图节点推进；tool 事件同时落为左栏可观测行。
     */
    handleEvent(ev: SseEvent): void {
      const d = ev.data as Record<string, unknown>;
      switch (ev.type) {
        case "sse.open":
          this.connected = true;
          break;
        case "sse.reconnecting":
          this.connected = false;
          break;
        case "message.start":
          this.progress = freshProgress();
          break;
        case "intent.updated":
          this.progress.intent = "done";
          this.progress.constraints = "running";
          break;
        case "constraints.updated":
          this.constraints = d;
          this.progress.constraints = "done";
          this.progress.candidates = "running";
          break;
        case "tool.start": {
          const node = SKILL_TO_NODE[String(d.skill)] ?? "candidates";
          this.progress[node] = "running";
          this.pushMsg("tool", `▸ ${String(d.skill)} 执行中…`);
          break;
        }
        case "tool.end": {
          const node = SKILL_TO_NODE[String(d.skill)] ?? "candidates";
          this.progress[node] = "done";
          const next: Record<GraphNode, NodeState> = { ...this.progress };
          if (node === "candidates") next.spatial_join = "running";
          if (node === "spatial_join") next.decide = "running";
          this.progress = next;
          this.pushMsg("tool", `▸ ${String(d.skill)} 完成（${String(d.elapsed_ms ?? "?")} ms）`);
          break;
        }
        case "decision.updated":
          this.decision = d;
          this.progress.decide = "done";
          this.progress.render = "running";
          break;
        case "layer.update":
          this.progress.render = "done";
          this.progress.feedback = "running";
          break;
        case "map.view":
          this.viewEvent = { center: d.center as [number, number], zoom: Number(d.zoom) };
          break;
        case "interrupt.required":
          this.question = (d.question as Record<string, unknown>) ?? {};
          this.busy = false;
          break;
        case "message.end":
          if (!this.busy) this.progress.feedback = "done";
          break;
        case "error":
          this.error = String(d.message ?? "执行出错");
          this.busy = false;
          break;
        default:
          break; // state.delta / plan.updated / token：视图层暂不消费
      }
    },

    pushMsg(role: ChatMessage["role"], text: string): void {
      this.messages.push({ id: (msgSeq += 1), role, text });
    },

    fail(e: unknown): void {
      this.busy = false;
      this.error = e instanceof Error ? e.message : String(e);
    },
  },
});
