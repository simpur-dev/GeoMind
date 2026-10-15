/**
 * 高德 JS API 2.0 最小类型声明（仅覆盖本仓库用到的面）。
 *
 * 设计约束（架构 §9.5）：不装 @amap/amap-jsapi-loader 等官方 npm 包，
 * 走 CDN script 引入，类型手写最小集——底图是「可替换的外围」，不进内核。
 * 需要更多 API 时在此按需补声明，不开启全量 any。
 */

declare namespace AMap {
  interface LngLat {
    getLng(): number;
    getLat(): number;
  }

  interface MapOptions {
    center?: [number, number];
    zoom?: number;
    viewMode?: "2D" | "3D";
    mapStyle?: string;
    zooms?: [number, number];
  }

  interface Map {
    setZoomAndCenter(zoom: number, center: [number, number], immediately?: boolean): void;
    setCenter(center: [number, number]): void;
    getCenter(): LngLat;
    setZoom(zoom: number): void;
    getZoom(): number;
    destroy(): void;
    on(type: string, fn: (ev: unknown) => void): void;
  }

  interface MarkerOptions {
    position: [number, number];
    content?: string | HTMLElement;
    title?: string;
    extData?: unknown;
    anchor?: string;
    offset?: [number, number];
    zIndex?: number;
  }

  interface Marker {
    on(type: string, fn: (ev: unknown) => void): void;
    getExtData(): unknown;
    setMap(map: Map | null): void;
  }

  interface Bounds {
    getNorthEast(): LngLat;
    getSouthWest(): LngLat;
  }

  interface RectangleOptions {
    bounds: [[number, number], [number, number]];
    strokeColor?: string;
    strokeWeight?: number;
    fillColor?: string;
    fillOpacity?: number;
    bubble?: boolean;
  }

  interface Rectangle {
    setMap(map: Map | null): void;
  }

  interface HeatMapOptions {
    radius?: number;
    opacity?: [number, number];
    gradient?: Record<number, string>;
  }

  interface HeatMap {
    setDataSet(data: { data: { lng: number; lat: number; count: number }[]; max: number }): void;
    setOptions(opts: HeatMapOptions): void;
    show(): void;
    hide(): void;
  }

  interface MouseTool {
    rectangle(fn: (rect: { getBounds(): Bounds }) => void): void;
    close(clear?: boolean): void;
  }

  /** CDN 脚本注入后的运行时命名空间（window.AMap）。 */
  interface Runtime {
    Map: { new (container: HTMLElement, opts?: MapOptions): Map };
    Marker: { new (opts: MarkerOptions): Marker };
    Rectangle: { new (opts: RectangleOptions): Rectangle };
    HeatMap: { new (map: Map, opts?: HeatMapOptions): HeatMap };
    MouseTool: { new (map: Map): MouseTool };
    plugin(name: string | string[], cb: () => void): void;
  }
}

interface Window {
  /** JS API 2.0 安全密钥（须在脚本加载前设置）。 */
  _AMapSecurityConfig?: { securityJsCode: string };
  AMap?: AMap.Runtime;
}
