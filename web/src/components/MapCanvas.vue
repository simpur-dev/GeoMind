<!--
地图主区（架构 §9.2 中栏）：承载 MapAdapter（高德 / 无 key 降级两档，§9.4）。

职责边界（架构 §9.6）：只做「渲染 + 手势采集」——
- 渲染：消费权威 Map Spec，按 diffLayers 增量更新（§4.2 铁律：只动变化图层）；
- 手势：点选 / 框选几何原样回传（GCJ-02），翻译与裁决全在后端。
不含任何业务判断：颜色 / 半径 / 数值键全部来自 Map Spec 的 style 声明。
-->
<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";

import { diffLayers, type LayerDiff } from "@/api/mapspec";
import type { FeatureCollection, GeoJsonFeature, LayerSpec, MapSpec } from "@/api/types";
import {
  AMapAdapter,
  isAmapKeyConfigured,
  loadAmapSdk,
  registerAmapRenderers,
} from "@/layers/amap";
import { FallbackMapAdapter, registerFallbackRenderers } from "@/layers/fallback";
import { createRenderer, type LayerRenderer, type MapAdapter } from "@/layers/registry";
import { useSessionStore } from "@/stores/session";

const store = useSessionStore();
const host = ref<HTMLDivElement | null>(null);
const mode = ref<"loading" | "amap" | "fallback" | "error">("loading");
const modeNote = ref("");
const drawMode = ref(false);

let adapter: MapAdapter | null = null;
let amapAdapter: AMapAdapter | null = null;
/** 已渲染图层 → 渲染器实例（增量更新的摘除句柄）。 */
const rendererCache = new Map<string, LayerRenderer>();
/** 已渲染图层 → 上次渲染用的数据引用（数据重拉即重绘的判定依据）。 */
const appliedData = new Map<string, FeatureCollection>();
const renderedIds = new Set<string>();
let lastRendered: MapSpec | null = null;
let lastView = "";

onMounted(async () => {
  const key = import.meta.env.VITE_AMAP_KEY as string | undefined;
  if (isAmapKeyConfigured(key)) {
    try {
      const amap = await loadAmapSdk(key);
      adapter = new AMapAdapter(host.value!, amap);
      amapAdapter = adapter as AMapAdapter;
      registerAmapRenderers(amapAdapter);
      mode.value = "amap";
    } catch (e) {
      // §9.4：高德通道失败自动降级，同一份 Map Spec 的第二种实现
      adapter = new FallbackMapAdapter(host.value!);
      registerFallbackRenderers(adapter as FallbackMapAdapter);
      mode.value = "fallback";
      modeNote.value = `高德底图加载失败（${e instanceof Error ? e.message : e}），已降级`;
    }
  } else {
    adapter = new FallbackMapAdapter(host.value!);
    registerFallbackRenderers(adapter as FallbackMapAdapter);
    mode.value = "fallback";
    modeNote.value = "未配置高德 key，当前为无底图降级模式（.env 填 VITE_AMAP_KEY 后自动升级）";
  }

  adapter.onPick((f: GeoJsonFeature) => void store.feedbackPointSelect(f));
  adapter.onDraw((poly) => void store.feedbackPolygon(poly));

  // 适配器就绪前 mapSpec / viewEvent 可能已到达，补一次镜头与渲染
  applyCamera(store.mapSpec);
  if (store.mapSpec) syncLayers();
});

onBeforeUnmount(() => {
  if (amapAdapter) amapAdapter.map.destroy();
  store.dispose();
});

// ---------------------------------------------------------------------------
// Map Spec 增量渲染（§4.2：只对变化图层做更新，不整图重绘）
// ---------------------------------------------------------------------------
watch(() => store.renderTick, syncLayers);
watch(() => store.viewEvent, () => applyCamera(null));

function syncLayers(): void {
  if (!adapter || !store.mapSpec) return;
  const spec = store.mapSpec;
  const diff: LayerDiff = diffLayers(lastRendered, spec);
  lastRendered = spec;

  // 两类变化都要动：① 图层声明变了（spec diff，§4.2 铁律）；
  // ② 声明没变但数据重拉了（候选集更新时 layer id/kind/style 原样，
  //    只有 GeoJSON 内容变——按引用比较，避免残显上一轮几何）
  const needsAction = new Set([...diff.added.map((l) => l.id), ...diff.updated, ...diff.removed]);

  for (const layer of spec.layers) {
    const visible = store.visibleLayers[layer.id] ?? true;
    const data = store.layerData[layer.id] ?? null;
    const shouldRender = visible && data !== null;
    const wasRendered = renderedIds.has(layer.id);
    const dataChanged = data !== appliedData.get(layer.id);

    if (shouldRender && (!wasRendered || needsAction.has(layer.id) || dataChanged)) {
      applyLayer(layer, data);
    } else if (!shouldRender && wasRendered) {
      // 开关关闭 / stale 失效 / 数据拉取失败：先摘旧几何（设计 D3）
      removeLayer(layer.id);
    }
  }
  for (const id of diff.removed) removeLayer(id);
}

function applyLayer(spec: LayerSpec, data: FeatureCollection): void {
  let renderer = rendererCache.get(spec.id);
  if (!renderer) {
    const created = createRenderer(spec, adapter!);
    if (!created) {
      console.warn(`[map] 未注册的 kind: ${spec.kind}（图层 ${spec.id} 跳过）`);
      return;
    }
    renderer = created;
    rendererCache.set(spec.id, renderer);
  }
  renderer.apply(adapter!, spec, data);
  appliedData.set(spec.id, data);
  renderedIds.add(spec.id);
}

function removeLayer(id: string): void {
  const spec = store.mapSpec?.layers.find((l) => l.id === id) ?? lastRendered?.layers.find((l) => l.id === id);
  const renderer = rendererCache.get(id);
  if (renderer && spec) renderer.remove(adapter!, spec);
  rendererCache.delete(id);
  appliedData.delete(id);
  renderedIds.delete(id);
}

function applyCamera(spec: MapSpec | null): void {
  const view = spec?.map_view ?? store.viewEvent;
  if (!adapter || !view) return;
  const sig = JSON.stringify(view);
  if (sig === lastView) return;
  lastView = sig;
  adapter.setView(view.center, view.zoom);
}

// ---------------------------------------------------------------------------
// 交互工具条
// ---------------------------------------------------------------------------
function toggleDraw(): void {
  if (!amapAdapter) return;
  drawMode.value = !drawMode.value;
  amapAdapter.setDrawMode(drawMode.value);
}

/** 列表 ↔ 地图高亮联动（§9.2 右栏）：两档模式统一走 CSS。 */
watch(() => store.selectedPoiId, (id, prev) => {
  if (!adapter) return;
  if (prev !== undefined && prev !== null) {
    adapter.el.querySelector(`[data-poi-id="${prev}"]`)?.classList.remove("gm-selected");
  }
  if (id !== null) {
    adapter.el.querySelector(`[data-poi-id="${id}"]`)?.classList.add("gm-selected");
  }
});
</script>

<template>
  <div class="map-wrap">
    <div ref="host" class="map-host" />

    <div class="map-topbar">
      <span class="badge" :class="`badge-${mode}`">
        {{ mode === "amap" ? "高德底图 GCJ-02" : mode === "fallback" ? "降级画布（无底图）" : "地图加载中…" }}
      </span>
      <span v-if="modeNote" class="map-note">{{ modeNote }}</span>
    </div>

    <div class="map-tools">
      <label v-for="l in store.mapSpec?.layers ?? []" :key="l.id" class="layer-toggle">
        <input
          type="checkbox"
          :checked="store.visibleLayers[l.id] ?? true"
          @change="store.toggleLayer(l.id)"
        >
        {{ l.id === "l_heat" ? "热力" : l.id === "l_candidates" ? "POI" : l.id }}
      </label>

      <button
        v-if="mode === 'amap'"
        class="draw-btn"
        :class="{ active: drawMode }"
        title="框选后回传为结构化约束，后端重跑一轮"
        @click="toggleDraw"
      >
        ▭ 框选
      </button>
      <span v-else-if="mode === 'fallback'" class="draw-hint">Shift + 拖拽 = 框选</span>
    </div>
  </div>
</template>

<style scoped>
.map-wrap { position: relative; width: 100%; height: 100%; overflow: hidden; }
.map-host { position: absolute; inset: 0; }

.map-topbar {
  position: absolute; top: 8px; left: 8px; z-index: 5;
  display: flex; align-items: center; gap: 8px; max-width: calc(100% - 16px);
}
.badge {
  padding: 2px 8px; border-radius: 4px; font-size: 12px;
  background: #fff; border: 1px solid #d0d7de; color: #57606a;
}
.badge-amap { border-color: #1677ff; color: #1677ff; }
.map-note { font-size: 12px; color: #9a6700; background: #fff8c5; border: 1px solid #d4a72c66;
  border-radius: 4px; padding: 2px 8px; }

.map-tools {
  position: absolute; bottom: 12px; left: 12px; z-index: 5;
  display: flex; align-items: center; gap: 10px;
  background: #fffffff2; border: 1px solid #d0d7de; border-radius: 6px;
  padding: 6px 10px; font-size: 12px; color: #1f2328;
}
.layer-toggle { display: inline-flex; align-items: center; gap: 4px; cursor: pointer; user-select: none; }
.draw-btn {
  border: 1px solid #1677ff; color: #1677ff; background: #fff;
  border-radius: 4px; padding: 2px 8px; cursor: pointer; font-size: 12px;
}
.draw-btn.active { background: #1677ff; color: #fff; }
.draw-hint { color: #57606a; }

/* 列表 ↔ 地图高亮联动（fallback circle 与 amap HTML marker 通吃） */
.map-host :deep(.gm-selected) { stroke: #ff5630; stroke-width: 3; }
.map-host :deep(.gm-poi.gm-selected .gm-poi-dot) { box-shadow: 0 0 0 3px #ff563066; }
.map-host :deep(.gm-poi) { display: flex; align-items: center; gap: 3px; }
.map-host :deep(.gm-poi-dot) { display: inline-block; border-radius: 50%; border: 1.5px solid #fff; }
.map-host :deep(.gm-poi-label) {
  font-size: 11px; color: #1f2937; background: #fffffff2;
  padding: 0 3px; border-radius: 3px; white-space: nowrap;
}
</style>
