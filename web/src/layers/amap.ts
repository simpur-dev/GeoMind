/**
 * 高德底图适配器（有 key 模式）。
 *
 * 与 fallback.ts 的 SVG 画布实现同一 MapAdapter 接口（架构 §9.4 两档
 * 零成本切换）：渲染器不感知底层是高德还是 SVG，同一份 Map Spec 消费。
 *
 * SDK 加载：CDN script 注入（架构 §9.5，不装官方 npm loader）；
 * key 合规：占位符/过短 key 一律视为未配置（不内置有效 key），
 * 安全密钥 securityJsCode 若有则经 window._AMapSecurityConfig 下发。
 */
import { isPoint } from "@/api/types";
import type { FeatureCollection, GeoJsonFeature, LayerSpec } from "@/api/types";
import type { LayerRenderer, MapAdapter } from "./registry";
import { registerRenderer } from "./registry";

const AMAP_VERSION = "2.0";
const AMAP_URL = (key: string) =>
  `https://webapi.amap.com/maps?v=${AMAP_VERSION}&key=${encodeURIComponent(key)}`
  + `&plugin=AMap.MouseTool,AMap.HeatMap`;
const SDK_TIMEOUT_MS = 12_000;

/** key 是否真的可用（占位符与空串都算未配置，走降级画布）。 */
export function isAmapKeyConfigured(key: string | undefined): key is string {
  return !!key && key.length >= 32 && !key.startsWith("Please apply");
}

let sdkPromise: Promise<AMap.Runtime> | null = null;

/** 注入 CDN script 并等待 window.AMap 就绪（结果缓存，多实例共用）。 */
export function loadAmapSdk(key: string): Promise<AMap.Runtime> {
  if (sdkPromise) return sdkPromise;
  sdkPromise = new Promise((resolve, reject) => {
    const scode = import.meta.env.VITE_AMAP_SECURITY_JSCODE as string | undefined;
    if (scode) window._AMapSecurityConfig = { securityJsCode: scode };

    const timer = setTimeout(() => reject(new Error("高德 SDK 加载超时")), SDK_TIMEOUT_MS);
    const script = document.createElement("script");
    script.src = AMAP_URL(key);
    script.onload = () => {
      clearTimeout(timer);
      if (window.AMap) resolve(window.AMap);
      else reject(new Error("高德 SDK 加载失败（window.AMap 缺失）"));
    };
    script.onerror = () => {
      clearTimeout(timer);
      sdkPromise = null; // 允许下次重试
      reject(new Error("高德 SDK 网络错误"));
    };
    document.head.appendChild(script);
  });
  return sdkPromise;
}

// ---------------------------------------------------------------------------
// 适配器
// ---------------------------------------------------------------------------
export class AMapAdapter implements MapAdapter {
  readonly mode = "amap" as const;
  readonly el: HTMLElement;
  readonly map: AMap.Map;
  readonly runtime: AMap.Runtime;

  private pickFn: ((f: GeoJsonFeature) => void) | null = null;
  private drawFn: ((p: [number, number][]) => void) | null = null;
  private mouseTool: AMap.MouseTool | null = null;

  constructor(container: HTMLElement, amap: AMap.Runtime,
              center: [number, number] = [104.0817, 30.657], zoom = 13) {
    this.runtime = amap;
    this.el = container;
    this.map = new amap.Map(container, { center, zoom, viewMode: "2D" });
  }

  setView(center: [number, number], zoom: number): void {
    this.map.setZoomAndCenter(zoom, center);
  }

  onPick(fn: (f: GeoJsonFeature) => void): void { this.pickFn = fn; }
  onDraw(fn: (p: [number, number][]) => void): void { this.drawFn = fn; }

  /** 渲染器点选回抛（marker click → emitPick → point_select 回传）。 */
  emitPick(feature: GeoJsonFeature): void {
    this.pickFn?.(feature);
  }

  /** 框选模式开关（地图主区按钮触发；与 fallback 的 Shift+拖拽同走 onDraw）。 */
  setDrawMode(on: boolean): void {
    if (!on) {
      this.mouseTool?.close(false);
      this.mouseTool = null;
      return;
    }
    this.mouseTool = new this.runtime.MouseTool(this.map);
    this.mouseTool.rectangle((rect) => {
      const b = rect.getBounds();
      const ne = b.getNorthEast();
      const sw = b.getSouthWest();
      const polygon: [number, number][] = [
        [sw.getLng(), sw.getLat()], [ne.getLng(), sw.getLat()],
        [ne.getLng(), ne.getLat()], [sw.getLng(), ne.getLat()],
        [sw.getLng(), sw.getLat()],
      ];
      this.mouseTool = null;
      this.drawFn?.(polygon);
    });
  }
}

// ---------------------------------------------------------------------------
// 渲染器：POI（HTML marker，点选/标签）与热力（HeatMap 插件，失败降级方块）
// ---------------------------------------------------------------------------
class AmapPoiRenderer implements LayerRenderer {
  readonly kind = "poi" as const;
  private markers: AMap.Marker[] = [];

  constructor(private readonly adapter: AMapAdapter) {}

  apply(_map: MapAdapter, spec: LayerSpec, data: FeatureCollection): void {
    this.remove();
    const color = String(spec.style.color ?? "#1677FF");
    const radius = Number(spec.style.radius ?? 6);
    const labelBy = Boolean(spec.style.labelBy);
    for (const f of data.features) {
      if (!isPoint(f.geometry)) continue;          // 只渲点几何，其他几何不猜坐标
      const [lng, lat] = f.geometry.coordinates;
      const name = String(f.properties.name ?? "");
      const html = document.createElement("div");
      html.className = "gm-poi";
      html.dataset.poiId = String(f.properties.poi_id ?? "");
      html.innerHTML =
        `<span class="gm-poi-dot" style="background:${color};width:${radius * 2}px;height:${radius * 2}px"></span>`
        + (labelBy ? `<span class="gm-poi-label">${name}</span>` : "");
      const m = new this.adapter.runtime.Marker({
        position: [lng, lat], content: html, extData: f,
        anchor: "bottom-center", zIndex: 120,
      });
      m.on("click", () => this.adapter.emitPick(f));
      this.adapter.map.add(m);
      this.markers.push(m);
    }
  }

  remove(): void {
    for (const m of this.markers) m.setMap(null);
    this.markers = [];
  }
}

class AmapHeatmapRenderer implements LayerRenderer {
  readonly kind = "heatmap" as const;
  private heat: AMap.HeatMap | null = null;
  private rects: AMap.Rectangle[] = [];

  constructor(private readonly adapter: AMapAdapter) {}

  apply(_map: MapAdapter, spec: LayerSpec, data: FeatureCollection): void {
    this.remove();
    const key = String(spec.style.valueKey ?? "flow");
    const radius = Number(spec.style.radius ?? 24);
    const points: { lng: number; lat: number; count: number }[] = [];
    for (const f of data.features) {
      if (!isPoint(f.geometry)) continue;      // 网格中心点是点几何
      points.push({
        lng: f.geometry.coordinates[0],
        lat: f.geometry.coordinates[1],
        count: Number(f.properties[key] ?? 0),
      });
    }
    const max = Math.max(1, ...points.map((p) => p.count));

    if (typeof this.adapter.runtime.HeatMap === "function") {
      this.heat = new this.adapter.runtime.HeatMap(this.adapter.map, {
        radius,
        opacity: [0, 0.8],
        gradient: { 0.3: "#2E7CF6", 0.5: "#32C5A0", 0.7: "#F6C344", 1.0: "#FF5630" },
      });
      this.heat.setDataSet({ data: points, max });
      return;
    }
    // 降级：HeatMap 插件不可达时用半透明方块（同一 kind 两种实现，§9.4）
    const sizeDeg = 0.0012;
    for (const p of points) {
      const r = new this.adapter.runtime.Rectangle({
        bounds: [[p.lng - sizeDeg, p.lat - sizeDeg], [p.lng + sizeDeg, p.lat + sizeDeg]],
        fillColor: "#FF5630", fillOpacity: 0.15 + 0.75 * (p.count / max),
        strokeWeight: 0, bubble: true,
      });
      r.setMap(this.adapter.map);
      this.rects.push(r);
    }
  }

  remove(): void {
    this.heat?.hide();
    this.heat = null;
    for (const r of this.rects) r.setMap(null);
    this.rects = [];
  }
}

/**
 * 高德颜色归一：把后端 style 里的 rgba() 串 / 3 位 hex 拆成 AMap 可靠解析的
 * 「6 位 hex + 独立 opacity」。AMap 2.0 对 rgba() 填充串实测整个面不可见，
 * 不能直接透传（§9.3 style 声明格式不因此受限，归一是渲染器的本分）。
 */
function normalizeAmapColor(c: string): { color: string; alpha: number } {
  const rgba = c.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/i);
  if (rgba) {
    const hex = (n: string) => Number(n).toString(16).padStart(2, "0");
    return {
      color: `#${hex(rgba[1])}${hex(rgba[2])}${hex(rgba[3])}`,
      alpha: rgba[4] !== undefined ? Number(rgba[4]) : 1,
    };
  }
  const short = c.match(/^#([0-9a-f])([0-9a-f])([0-9a-f])$/i);
  if (short) return { color: `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`, alpha: 1 };
  return { color: c, alpha: 1 };
}

/**
 * 面渲染器：polygon 与 isochrone 共用 AMap.Polygon（2.0 核心类，不需要插件）。
 * 差别只在后端 style 是否声明 dash；颜色/线宽全部来自 spec.style（§9.3）。
 */
class AmapPolygonRenderer implements LayerRenderer {
  readonly kind: LayerSpec["kind"];
  private polys: AMap.Polygon[] = [];

  constructor(private readonly adapter: AMapAdapter, kind: "polygon" | "isochrone") {
    this.kind = kind;
  }

  apply(_map: MapAdapter, spec: LayerSpec, data: FeatureCollection): void {
    this.remove();
    const dashed = Boolean(spec.style.dash);
    const fill = normalizeAmapColor(String(spec.style.fill ?? "#338AFF"));
    const stroke = normalizeAmapColor(String(spec.style.stroke ?? "#1677FF"));
    for (const f of data.features) {
      if (f.geometry.type !== "Polygon") continue;
      const poly = new this.adapter.runtime.Polygon({
        path: f.geometry.coordinates,
        fillColor: fill.color,
        fillOpacity: Number(spec.style.fillOpacity ?? fill.alpha),
        strokeColor: stroke.color,
        strokeWeight: Number(spec.style.width ?? 2),
        strokeStyle: dashed ? "dashed" : "solid",
        strokeDasharray: dashed ? [6, 4] : undefined,
        bubble: true,
        zIndex: this.kind === "isochrone" ? 40 : 60,
      });
      poly.setMap(this.adapter.map);
      this.polys.push(poly);
    }
  }

  remove(): void {
    for (const p of this.polys) p.setMap(null);
    this.polys = [];
  }
}

/** 线渲染器（kind=route）：AMap.Polyline 描边 + 起/终点标记。 */
class AmapRouteRenderer implements LayerRenderer {
  readonly kind = "route" as const;
  private lines: AMap.Polyline[] = [];
  private ends: AMap.Marker[] = [];

  constructor(private readonly adapter: AMapAdapter) {}

  apply(_map: MapAdapter, spec: LayerSpec, data: FeatureCollection): void {
    this.remove();
    const color = String(spec.style.color ?? "#3366cc");
    const width = Number(spec.style.width ?? 4);
    const dot = Number(spec.style.endpointRadius ?? 5);

    for (const f of data.features) {
      if (f.geometry.type !== "LineString") continue;
      const line = new this.adapter.runtime.Polyline({
        path: f.geometry.coordinates,
        strokeColor: color,
        strokeWeight: width,
        strokeOpacity: 0.9,
        lineJoin: "round",
        bubble: true,
      });
      line.setMap(this.adapter.map);
      this.lines.push(line);

      // 起点实心、终点空心（HTML marker，与 poi 渲染器同一写法，不引额外 CSS）
      const ends: [number[], boolean][] = [[f.geometry.coordinates[0], false],
        [f.geometry.coordinates[f.geometry.coordinates.length - 1], true]];
      for (const [pt, hollow] of ends) {
        const html = document.createElement("span");
        html.style.cssText = `display:inline-block;width:${dot * 2}px;height:${dot * 2}px;`
          + `border-radius:50%;background:${hollow ? "#fff" : color};border:2px solid ${color}`;
        const m = new this.adapter.runtime.Marker({
          position: [pt[0], pt[1]], content: html, anchor: "center", zIndex: 130,
        });
        m.setMap(this.adapter.map);
        this.ends.push(m);
      }
    }
  }

  remove(): void {
    for (const l of this.lines) l.setMap(null);
    for (const m of this.ends) m.setMap(null);
    this.lines = [];
    this.ends = [];
  }
}

/** 注册高德模式渲染器（MapCanvas 在 SDK 加载成功后调用）。 */
export function registerAmapRenderers(map: AMapAdapter): void {
  registerRenderer("poi", () => new AmapPoiRenderer(map));
  registerRenderer("heatmap", () => new AmapHeatmapRenderer(map));
  registerRenderer("polygon", () => new AmapPolygonRenderer(map, "polygon"));
  registerRenderer("isochrone", () => new AmapPolygonRenderer(map, "isochrone"));
  registerRenderer("route", () => new AmapRouteRenderer(map));
}
