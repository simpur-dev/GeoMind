<!--
三栏工作台（架构 §9.2）：左对话 ~22% / 中地图自适应 / 右结果 ~24% + 底部进度条。
小屏响应式降级：右栏折到底部堆叠，地图主区保持全屏可用（§9.2 末条）。
头部会话信息 + 连接状态；后端未就绪时给出明确引导而非白屏。
-->
<script setup lang="ts">
import { onMounted, ref } from "vue";

import ChatPanel from "@/components/ChatPanel.vue";
import MapCanvas from "@/components/MapCanvas.vue";
import ProgressBar from "@/components/ProgressBar.vue";
import ResultPanel from "@/components/ResultPanel.vue";
import { useSessionStore } from "@/stores/session";

const store = useSessionStore();
const bootError = ref("");

onMounted(async () => {
  try {
    await store.init();
  } catch (e) {
    bootError.value = e instanceof Error ? e.message : String(e);
  }
});
</script>

<template>
  <div class="workbench">
    <header class="topbar">
      <span class="logo">GeoMind</span>
      <span class="scene">● 选址分析</span>
      <span v-if="store.sessionId" class="sid">会话 {{ store.sessionId }}</span>
      <span class="conn" :class="{ on: store.connected }">
        {{ store.connected ? "SSE 已连接" : "SSE 未连接" }}
      </span>
    </header>

    <div v-if="bootError" class="boot-error">
      后端连接失败：{{ bootError }}
      <div class="boot-hint">请先启动后端（uvicorn backend.app:app --port 8000）后刷新页面。</div>
    </div>

    <main v-else class="columns">
      <ChatPanel class="col-chat" />
      <div class="col-map"><MapCanvas /></div>
      <ResultPanel class="col-result" />
    </main>

    <ProgressBar />
  </div>
</template>

<style scoped>
.workbench {
  display: flex; flex-direction: column; height: 100vh;
  font-size: 14px; color: #1f2328; background: #f6f8fa;
}

.topbar {
  display: flex; align-items: center; gap: 14px;
  background: #fff; border-bottom: 1px solid #d0d7de; padding: 6px 14px;
}
.logo { font-weight: 700; letter-spacing: 0.5px; }
.scene, .sid { color: #57606a; font-size: 13px; }
.conn { margin-left: auto; font-size: 12px; color: #8b949e; }
.conn.on { color: #1a7f37; }

.boot-error {
  flex: 1; display: flex; flex-direction: column; gap: 8px;
  align-items: center; justify-content: center; color: #cf222e; font-size: 14px;
}
.boot-hint { color: #57606a; font-size: 13px; }

.columns {
  flex: 1; display: flex; min-height: 0;
}
.col-chat { flex: 0 0 22%; min-width: 220px; }
.col-map { flex: 1; min-width: 0; }
.col-result { flex: 0 0 24%; min-width: 240px; }

/* 小屏降级：右栏折到底部堆叠（§9.2） */
@media (max-width: 900px) {
  .columns { flex-direction: column; overflow-y: auto; }
  .col-map { flex: 0 0 55vh; order: 1; }
  .col-chat { flex: none; order: 2; border-right: none; border-top: 1px solid #d0d7de; }
  .col-result { flex: none; order: 3; border-left: none; border-top: 1px solid #d0d7de; }
}
</style>
