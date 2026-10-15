/**
 * 图层渲染器注册表（架构 §9.3：前端按 Map Spec 的 kind 注册渲染器）。
 *
 * 每个 kind 一个渲染工厂；新增 kind = 在 registry 里加一行 + 一个渲染器文件，
 * 核心零改动。渲染器只消费 Map Spec 的声明（style/source），不含业务判断。
 */
import type { FeatureCollection, GeoJsonFeature, LayerSpec, MapSpec } from "@/api/types";

/** 渲染器统一接口：apply = 全量应用一个图层；remove = 摘除该图层。 */
export interface LayerRenderer {
  readonly kind: LayerSpec["kind"];
  /** 拉取/接收图层数据并渲染。alreadyRendered 为 true 时只更新几何。 */
  apply(map: MapAdapter, spec: LayerSpec, data: FeatureCollection): void;
  remove(map: MapAdapter, spec: LayerSpec): void;
}

/**
 * 地图适配层：高德底图模式与无 key 降级画布都实现同一接口，
 * 渲染器不感知底层是高德还是 SVG（架构 §9.4 两档切换零成本）。
 */
export interface MapAdapter {
  readonly mode: "amap" | "fallback";
  /** 镜头定位（GCJ-02）。 */
  setView(center: [number, number], zoom: number): void;
  /** 点选回调注册（用于 point_select 回传）。 */
  onPick(fn: (feature: GeoJsonFeature) => void): void;
  /** 框选完成回调（顶点为 GCJ-02）。 */
  onDraw(fn: (polygon: [number, number][]) => void): void;
  /**
   * 渲染器回抛点选（amap 模式：marker 点击由渲染器监听后调用；
   * fallback 模式走 DOM data 属性，无需实现）。
   */
  emitPick?(feature: GeoJsonFeature): void;
  readonly el: HTMLElement;
}

const registry = new Map<LayerSpec["kind"], (map: MapAdapter) => LayerRenderer>();

export function registerRenderer(
  kind: LayerSpec["kind"],
  factory: (map: MapAdapter) => LayerRenderer,
): void {
  registry.set(kind, factory);
}

export function createRenderer(
  spec: LayerSpec,
  map: MapAdapter,
): LayerRenderer | null {
  const factory = registry.get(spec.kind);
  return factory ? factory(map) : null;
}

export function supportedKinds(): LayerSpec["kind"][] {
  return [...registry.keys()];
}

/** 渲染整份 Map Spec：按 diff 只动变化图层（架构 §4.2）。 */
export function renderMapSpec(
  map: MapAdapter,
  prev: MapSpec | null,
  next: MapSpec,
  dataOf: (spec: LayerSpec) => FeatureCollection | null,
): void {
  // diff 在调用方（useMapRenderer）完成，这里只负责把单个图层落到地图上
  for (const spec of next.layers) {
    const renderer = createRenderer(spec, map);
    if (!renderer) {
      console.warn(`[mapspec] 未注册的 kind: ${spec.kind}（图层 ${spec.id} 跳过）`);
      continue;
    }
    const data = dataOf(spec);
    if (data) renderer.apply(map, spec, data);
  }
}
