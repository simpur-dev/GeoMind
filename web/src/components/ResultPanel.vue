<!--
右栏（架构 §9.2，~24%）：由 Map Spec 的 panels[] 声明驱动（kind: ranking/metrics），
数据经 data_ref 从视图缓存解析；候选行点击 ↔ 地图高亮联动；点选手势回显候选详情。
前端不计算：所有数值都是后端算好的展示。
-->
<script setup lang="ts">
import { computed } from "vue";

import type { Candidate } from "@/api/types";
import { useSessionStore } from "@/stores/session";

const store = useSessionStore();

const topGrid = computed(() => (store.decision.top_grid ?? null) as Record<string, unknown> | null);
const summary = computed(() => String(store.decision.summary ?? ""));

function onRowClick(c: Candidate): void {
  store.selectPoi(c.poi_id);
}
</script>

<template>
  <aside class="result-panel">
    <template v-if="store.mapSpec">
      <section v-for="p in store.mapSpec.panels" :key="p.id" class="panel">
        <h3 class="panel-title">{{ p.title || p.id }}</h3>

        <!-- kind=ranking：候选列表（↔ 地图高亮联动） -->
        <table v-if="p.kind === 'ranking'" class="rank-table">
          <thead>
            <tr><th>名称</th><th>评分</th><th>距离m</th><th>语义</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="c in store.candidates"
              :key="c.poi_id"
              :class="{ selected: store.selectedPoiId === c.poi_id }"
              @click="onRowClick(c)"
            >
              <td class="name" :title="c.name">{{ c.name }}</td>
              <td>{{ c.rating ?? "—" }}</td>
              <td>{{ c.dist_m != null ? Math.round(c.dist_m) : "—" }}</td>
              <td>{{ c.sem_dist != null ? c.sem_dist.toFixed(3) : "—" }}</td>
            </tr>
            <tr v-if="!store.candidates.length">
              <td colspan="4" class="empty">本轮无候选</td>
            </tr>
          </tbody>
        </table>

        <!-- kind=metrics：决策指标 + 结论（可解释） -->
        <div v-else-if="p.kind === 'metrics'" class="metrics">
          <div v-if="topGrid" class="kv">
            <span class="k">Top 网格</span><span class="v">{{ topGrid.grid_id }}</span>
            <span class="k">客流</span><span class="v">{{ topGrid.flow ?? "—" }}</span>
            <span class="k">POI 数</span><span class="v">{{ topGrid.poi_cnt ?? "—" }}</span>
            <span class="k">均评分</span><span class="v">{{ topGrid.avg_rating ?? "—" }}</span>
          </div>
          <p v-if="summary" class="summary">{{ summary }}</p>
          <p v-if="!topGrid && !summary" class="empty">等待决策输出…</p>
        </div>
      </section>

      <!-- 点选手势回显（point_select → 候选详情） -->
      <section v-if="store.pickedCandidate" class="panel">
        <h3 class="panel-title">点选详情</h3>
        <div class="kv">
          <span class="k">POI</span><span class="v">{{ store.pickedCandidate.name }}</span>
          <span class="k">评分</span><span class="v">{{ store.pickedCandidate.rating ?? "—" }}</span>
          <span class="k">距离m</span>
          <span class="v">{{ store.pickedCandidate.dist_m != null ? Math.round(store.pickedCandidate.dist_m) : "—" }}</span>
        </div>
      </section>
    </template>

    <div v-else class="empty placeholder">
      提问后，Map Spec 的面板声明会驱动这里（候选排名 / 关键指标）。
    </div>
  </aside>
</template>

<style scoped>
.result-panel {
  height: 100%; overflow-y: auto; border-left: 1px solid #d0d7de;
  background: #fff; padding: 10px; display: flex; flex-direction: column; gap: 12px; min-width: 0;
}
.panel-title { font-size: 13px; font-weight: 600; margin: 0 0 6px; color: #1f2328; }

.rank-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.rank-table th {
  text-align: left; color: #57606a; font-weight: 500;
  border-bottom: 1px solid #d0d7de; padding: 3px 4px;
}
.rank-table td { padding: 4px; border-bottom: 1px solid #eaeef2; }
.rank-table tr { cursor: pointer; }
.rank-table tr:hover { background: #f6f8fa; }
.rank-table tr.selected { background: #e7f1ff; }
.rank-table .name { max-width: 0; width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.kv { display: grid; grid-template-columns: auto 1fr; gap: 2px 10px; font-size: 12px; }
.kv .k { color: #57606a; }
.kv .v { color: #1f2328; }

.summary { font-size: 12px; line-height: 1.6; margin: 8px 0 0; color: #1f2328;
  background: #f6f8fa; border-radius: 6px; padding: 6px 8px; }
.empty { color: #8b949e; font-size: 12px; text-align: center; padding: 8px 0; }
.placeholder { margin-top: 40px; text-align: center; line-height: 1.8; }
</style>
