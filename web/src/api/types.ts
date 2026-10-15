/**
 * Map Spec 契约类型（与 backend/api/contracts/map_spec.py 逐字段对齐）。
 *
 * 这是前后端契约的 TS 侧权威定义。改字段必须同步后端 Pydantic 模型，
 * 并由 tests/test_api_contract.py 的契约测试钉死。
 *
 * 坐标系：所有坐标为 GCJ-02（高德底图）。WGS84 → GCJ-02 转换在后端完成，
 * 前端零计算（架构 §9.6 职责边界表）。
 */

/** 图层 kind 注册表（架构 §9.3）。新增 kind = 在 layers/registry.ts 注册渲染器。 */
export type LayerKind =
  | "poi"
  | "heatmap"
  | "polygon"
  | "isochrone"
  | "route"
  | "trajectory"
  | "grid"
  | "marker";

export type LayerStatus = "ready" | "stale" | "error";
export type PanelKind = "ranking" | "metrics" | "text";

export interface MapView {
  center: [number, number];
  zoom: number;
  pitch: number;
}

export interface LayerSource {
  href?: string;
  type: "geojson" | "inline";
}

export interface LayerSpec {
  id: string;
  kind: LayerKind;
  source: LayerSource | null;
  data_ref: string | null;
  style: Record<string, unknown>;
  interactions: string[];
  status: LayerStatus;
  derived_from: string[];
}

export interface PanelSpec {
  id: string;
  kind: PanelKind;
  data_ref: string;
  title: string;
}

export interface MapSpec {
  version: string;
  map_view: MapView | null;
  layers: LayerSpec[];
  panels: PanelSpec[];
}

/** GeoJSON FeatureCollection（后端 /layers/{id} 返回，GCJ-02）。 */
export interface GeoJsonFeature {
  type: "Feature";
  geometry: { type: "Point"; coordinates: [number, number] };
  properties: Record<string, unknown>;
}

export interface FeatureCollection {
  type: "FeatureCollection";
  features: GeoJsonFeature[];
}

/** SSE 事件（架构 §9.3 事件类型表）。 */
export interface SseEvent {
  type: string;
  data: Record<string, unknown>;
}

/** 候选（与后端 PoiCandidate / ScoredCandidate 对齐）。 */
export interface Candidate {
  poi_id: number;
  name: string;
  category_id: number | null;
  rating: number | null;
  dist_m: number | null;
  sem_dist: number | null;
}

export interface GridAgg {
  grid_id: number;
  flow: number | null;
  poi_cnt: number;
  avg_rating: number | null;
}
