/**
 * 无底图降级渲染器（无高德 key 时启用）。
 *
 * 设计约束（架构 §9.1「三不原则」/ §9.4 两档零成本切换）：
 * - 消费同一份 Map Spec（kind/style 全部来自后端声明，不含写死的业务常量）；
 * - 只做投影与绘制，不做任何空间计算裁决；
 * - 坐标系与高德模式一致（GCJ-02 直绘，投影仅用于屏幕定位）。
 *
 * 投影：以 map_view.center 为中心的等距圆柱近似（局部范围误差可忽略），
 * 视野跨度按 zoom 估算（高德 zoom 语义近似：span ≈ 0.02° / 2^(zoom-13)）。
 */
import { isPoint } from "@/api/types";
import type { FeatureCollection, GeoJsonFeature, LayerSpec, MapView } from "@/api/types";
import type { LayerRenderer, MapAdapter } from "./registry";
import { registerRenderer } from "./registry";

const SVG_NS = "http://www.w3.org/2000/svg";

function spanDeg(zoom: number): number {
  return 0.02 / 2 ** (Math.max(3, Math.min(18, zoom)) - 13);
}

/** 把一段经纬度环/折线投影成 SVG path 的 d 属性。 */
function ringPath(map: FallbackMapAdapter, ring: [number, number][]): string {
  return ring
    .map(([lng, lat], i) => {
      const [x, y] = map.project(lng, lat);
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

/** SVG 属性里只允许后端声明的数值与颜色，缺省值仅用于「不至于看不见」。 */
function num(v: unknown, dflt: number): number {
  return typeof v === "number" && Number.isFinite(v) ? v : dflt;
}

/** 投影器：GCJ-02 → SVG 像素坐标（以中心为零点）。 */
class Projector {
  constructor(
    private readonly center: [number, number],
    private readonly span: number,
    private readonly w: number,
    private readonly h: number,
  ) {}

  project(lng: number, lat: number): [number, number] {
    const x = this.w / 2 + ((lng - this.center[0]) / this.span) * this.w;
    const y = this.h / 2 - ((lat - this.center[1]) / this.span) * this.h;
    return [x, y];
  }
}

/** 降级适配器：SVG 画布 + 框选 + 点选。 */
export class FallbackMapAdapter implements MapAdapter {
  readonly mode = "fallback" as const;
  readonly el: HTMLDivElement;
  private svg: SVGSVGElement;
  private view: MapView = { center: [104.0817, 30.657], zoom: 13, pitch: 0 };
  private pickFn: ((f: GeoJsonFeature) => void) | null = null;
  private drawFn: ((p: [number, number][]) => void) | null = null;
  /** 图层根节点：渲染器把自己的 <g> 挂在这里（必须公开）。 */
  readonly layersRoot: SVGGElement;
  private drawLayer: SVGGElement;
  private projector: Projector;
  private drawing = false;

  constructor(container: HTMLElement) {
    this.el = document.createElement("div");
    this.el.className = "map-fallback";
    this.el.style.cssText = "position:absolute;inset:0;background:#eef2f7;overflow:hidden";
    this.svg = document.createElementNS(SVG_NS, "svg") as SVGSVGElement;
    this.svg.setAttribute("width", "100%");
    this.svg.setAttribute("height", "100%");
    this.layersRoot = document.createElementNS(SVG_NS, "g") as SVGGElement;
    this.drawLayer = document.createElementNS(SVG_NS, "g") as SVGGElement;
    this.svg.appendChild(this.layersRoot);
    this.svg.appendChild(this.drawLayer);
    this.el.appendChild(this.svg);
    container.appendChild(this.el);

    const rect = () => ({ w: this.el.clientWidth || 800, h: this.el.clientHeight || 600 });
    this.projector = new Projector(this.view.center, spanDeg(this.view.zoom), rect().w, rect().h);
    this.bindDraw();
  }

  setView(center: [number, number], zoom: number): void {
    this.view = { center, zoom, pitch: 0 };
    this.projector = new Projector(center, spanDeg(zoom),
      this.el.clientWidth || 800, this.el.clientHeight || 600);
    this.redrawGrid();
  }

  onPick(fn: (f: GeoJsonFeature) => void): void { this.pickFn = fn; }
  onDraw(fn: (p: [number, number][]) => void): void { this.drawFn = fn; }

  /** 渲染器拿到的投影上下文。 */
  project(lng: number, lat: number): [number, number] {
    return this.projector.project(lng, lat);
  }

  private redrawGrid(): void {
    // 简单参考网格（帮助用户在无底图模式下建立空间感）
    this.svg.querySelectorAll(".ref-grid").forEach((n) => n.remove());
    const span = spanDeg(this.view.zoom);
    const step = span / 4;
    for (let i = 1; i < 4; i++) {
      const [x] = this.project(this.view.center[0] - span / 2 + step * i, this.view.center[1]);
      const line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("x1", String(x)); line.setAttribute("x2", String(x));
      line.setAttribute("y1", "0"); line.setAttribute("y2", "100%");
      line.setAttribute("stroke", "#d5dde8"); line.setAttribute("class", "ref-grid");
      this.svg.appendChild(line);
    }
  }

  private bindDraw(): void {
    let start: [number, number] | null = null;
    let rect: SVGRectElement | null = null;

    const toGeo = (ev: PointerEvent): [number, number] => {
      const r = this.svg.getBoundingClientRect();
      const px = ev.clientX - r.left, py = ev.clientY - r.top;
      const span = spanDeg(this.view.zoom);
      const lng = this.view.center[0] + ((px - r.width / 2) / r.width) * span;
      const lat = this.view.center[1] - ((py - r.height / 2) / r.height) * span;
      return [lng, lat];
    };

    this.svg.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0 || !ev.shiftKey) return; // Shift+拖拽 = 框选
      start = toGeo(ev);
      this.drawing = true;
      rect = document.createElementNS(SVG_NS, "rect") as SVGRectElement;
      rect.setAttribute("fill", "rgba(22,119,255,0.12)");
      rect.setAttribute("stroke", "#1677FF");
      this.drawLayer.appendChild(rect);
    });
    window.addEventListener("pointermove", (ev) => {
      if (!this.drawing || !start || !rect) return;
      const cur = toGeo(ev);
      const [x0, y0] = this.projector.project(start[0], start[1]);
      const [x1, y1] = this.projector.project(cur[0], cur[1]);
      rect.setAttribute("x", String(Math.min(x0, x1)));
      rect.setAttribute("y", String(Math.min(y0, y1)));
      rect.setAttribute("width", String(Math.abs(x1 - x0)));
      rect.setAttribute("height", String(Math.abs(y1 - y0)));
    });
    window.addEventListener("pointerup", (ev) => {
      if (!this.drawing || !start) return;
      const end = toGeo(ev);
      this.drawing = false;
      rect?.remove();
      const polygon: [number, number][] = [
        [start[0], start[1]], [end[0], start[1]],
        [end[0], end[1]], [start[0], end[1]], [start[0], start[1]],
      ];
      if (Math.abs(end[0] - start[0]) > 1e-5 && Math.abs(end[1] - start[1]) > 1e-5) {
        this.drawFn?.(polygon);
      }
      start = null; rect = null;
    });

    // 点选：点击 POI 圆点（坐标从 data 属性取真值，不造假几何）
    this.svg.addEventListener("click", (ev) => {
      const target = ev.target as SVGElement;
      const gid = target.getAttribute("data-poi-id");
      if (gid && this.pickFn) {
        const lng = Number(target.getAttribute("data-lng"));
        const lat = Number(target.getAttribute("data-lat"));
        this.pickFn({
          type: "Feature",
          geometry: { type: "Point", coordinates: [lng, lat] },
          properties: { poi_id: Number(gid), name: target.getAttribute("data-name") ?? "" },
        });
      }
    });
  }
}

/** POI 降级渲染：圆点 + 名称标签（style 全部来自后端声明）。 */
class FallbackPoiRenderer implements LayerRenderer {
  readonly kind = "poi" as const;
  private root: SVGGElement;

  constructor(private readonly map: FallbackMapAdapter) {
    this.root = document.createElementNS(SVG_NS, "g") as SVGGElement;
    this.map.layersRoot.appendChild(this.root);
  }

  apply(_map: MapAdapter, spec: LayerSpec, data: FeatureCollection): void {
    this.root.querySelectorAll("*").forEach((n) => n.remove());
    const radius = Number(spec.style.radius ?? 6);
    const color = String(spec.style.color ?? "#1677FF");
    for (const f of data.features) {
      if (!isPoint(f.geometry)) continue;          // 只渲点几何，其他几何不猜坐标
      const [x, y] = this.map.project(...f.geometry.coordinates);
      const c = document.createElementNS(SVG_NS, "circle");
      c.setAttribute("cx", String(x)); c.setAttribute("cy", String(y));
      c.setAttribute("r", String(radius));
      c.setAttribute("fill", color);
      c.setAttribute("stroke", "#fff"); c.setAttribute("stroke-width", "1.5");
      c.setAttribute("data-poi-id", String(f.properties.poi_id ?? ""));
      c.setAttribute("data-name", String(f.properties.name ?? ""));
      c.setAttribute("data-lng", String(f.geometry.coordinates[0]));
      c.setAttribute("data-lat", String(f.geometry.coordinates[1]));
      c.style.cursor = "pointer";
      this.root.appendChild(c);
      if (spec.style.labelBy) {
        const t = document.createElementNS(SVG_NS, "text");
        t.setAttribute("x", String(x + radius + 3)); t.setAttribute("y", String(y + 4));
        t.setAttribute("font-size", "11"); t.setAttribute("fill", "#1f2937");
        t.textContent = String(f.properties.name ?? "");
        this.root.appendChild(t);
      }
    }
  }

  remove(): void {
    this.root.querySelectorAll("*").forEach((n) => n.remove());
  }
}

/** 热力降级渲染：按 valueKey 归一化透明度的方块。 */
class FallbackHeatmapRenderer implements LayerRenderer {
  readonly kind = "heatmap" as const;
  private root: SVGGElement;

  constructor(private readonly map: FallbackMapAdapter) {
    this.root = document.createElementNS(SVG_NS, "g") as SVGGElement;
    this.map.layersRoot.appendChild(this.root);
  }

  apply(_map: MapAdapter, spec: LayerSpec, data: FeatureCollection): void {
    this.root.querySelectorAll("*").forEach((n) => n.remove());
    const key = String(spec.style.valueKey ?? "flow");
    const values = data.features.map((f) => Number(f.properties[key] ?? 0));
    const max = Math.max(1, ...values);
    const size = Number(spec.style.radius ?? 24) * 1.5;
    for (const f of data.features) {
      if (!isPoint(f.geometry)) continue;          // 网格中心点是点几何
      const v = Number(f.properties[key] ?? 0);
      const [x, y] = this.map.project(...f.geometry.coordinates);
      const r = document.createElementNS(SVG_NS, "rect");
      r.setAttribute("x", String(x - size / 2)); r.setAttribute("y", String(y - size / 2));
      r.setAttribute("width", String(size)); r.setAttribute("height", String(size));
      r.setAttribute("fill", `rgba(255,86,48,${(0.15 + 0.75 * (v / max)).toFixed(3)})`);
      r.setAttribute("data-grid-id", String(f.properties.grid_id ?? ""));
      this.root.appendChild(r);
    }
  }

  remove(): void {
    this.root.querySelectorAll("*").forEach((n) => n.remove());
  }
}

/**
 * 面渲染器：polygon 与 isochrone 共用同一实现，差别只在后端 style 是否声明 dash。
 * 样式全部消费 spec.style（架构 §9.3），前端不含业务常量。
 */
class FallbackPolygonRenderer implements LayerRenderer {
  readonly kind: LayerSpec["kind"];
  private readonly root: SVGGElement;

  constructor(private readonly map: FallbackMapAdapter, kind: "polygon" | "isochrone") {
    this.kind = kind;
    this.root = document.createElementNS(SVG_NS, "g") as SVGGElement;
    // 必须真的 append 进 DOM（此前画在游离 <g> 上导致白屏，见设计文档踩坑记录）
    this.map.layersRoot.appendChild(this.root);
  }

  apply(_map: MapAdapter, spec: LayerSpec, data: FeatureCollection): void {
    this.root.querySelectorAll("*").forEach((n) => n.remove());
    for (const f of data.features) {
      if (f.geometry.type !== "Polygon") continue;
      const p = document.createElementNS(SVG_NS, "path") as SVGPathElement;
      p.setAttribute("d", `${ringPath(this.map, f.geometry.coordinates[0])} Z`);
      p.setAttribute("fill", String(spec.style.fill ?? "#338AFF"));
      p.setAttribute("fill-opacity", String(num(spec.style.fillOpacity, num(spec.style.opacity, 1))));
      p.setAttribute("stroke", String(spec.style.stroke ?? "#1677FF"));
      p.setAttribute("stroke-width", String(num(spec.style.width, 2)));
      if (spec.style.dash) p.setAttribute("stroke-dasharray", "6 4");
      this.root.appendChild(p);
    }
  }

  remove(): void {
    this.root.querySelectorAll("*").forEach((n) => n.remove());
  }
}

/** 线渲染器（kind=route）：折线描边 + 起终点标记（§9.3 route 图层）。 */
class FallbackRouteRenderer implements LayerRenderer {
  readonly kind = "route" as const;
  private readonly root: SVGGElement;

  constructor(private readonly map: FallbackMapAdapter) {
    this.root = document.createElementNS(SVG_NS, "g") as SVGGElement;
    this.map.layersRoot.appendChild(this.root);
  }

  apply(_map: MapAdapter, spec: LayerSpec, data: FeatureCollection): void {
    this.root.querySelectorAll("*").forEach((n) => n.remove());
    const color = String(spec.style.color ?? "#3366cc");
    const width = num(spec.style.width, 4);
    const dot = num(spec.style.endpointRadius, 5);

    for (const f of data.features) {
      if (f.geometry.type !== "LineString") continue;
      const pts = f.geometry.coordinates;
      const line = document.createElementNS(SVG_NS, "polyline") as SVGPolylineElement;
      line.setAttribute("points", pts.map(([lng, lat]) => {
        const [x, y] = this.map.project(lng, lat);
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(" "));
      line.setAttribute("fill", "none");
      line.setAttribute("stroke", color);
      line.setAttribute("stroke-width", String(width));
      line.setAttribute("stroke-linejoin", "round");
      this.root.appendChild(line);

      // 起终点：首点实心、末点空心，半径来自后端 style 声明
      const endpoint = (pt: [number, number], hollow: boolean): void => {
        const [x, y] = this.map.project(pt[0], pt[1]);
        const c = document.createElementNS(SVG_NS, "circle") as SVGCircleElement;
        c.setAttribute("cx", String(x)); c.setAttribute("cy", String(y));
        c.setAttribute("r", String(dot));
        c.setAttribute("fill", hollow ? "#fff" : color);
        c.setAttribute("stroke", color);
        c.setAttribute("stroke-width", "2");
        this.root.appendChild(c);
      };
      endpoint(pts[0], false);
      endpoint(pts[pts.length - 1], true);
    }
  }

  remove(): void {
    this.root.querySelectorAll("*").forEach((n) => n.remove());
  }
}

/** 注册降级渲染器（amap 注册表未启用时由 MapCanvas 调用）。 */
export function registerFallbackRenderers(map: FallbackMapAdapter): void {
  registerRenderer("poi", () => new FallbackPoiRenderer(map));
  registerRenderer("heatmap", () => new FallbackHeatmapRenderer(map));
  registerRenderer("polygon", () => new FallbackPolygonRenderer(map, "polygon"));
  registerRenderer("isochrone", () => new FallbackPolygonRenderer(map, "isochrone"));
  registerRenderer("route", () => new FallbackRouteRenderer(map));
}
