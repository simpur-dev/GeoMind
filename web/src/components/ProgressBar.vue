<!--
底部进度条（架构 §9.2）：7 步图节点状态 + 错误横幅。
节点状态由 store 从 SSE 事件表推进（意图→约束→候选召回→空间连接→决策→渲染→反馈）。
不做假「中断」按钮：HITL 中断由后端 interrupt() 触发，前端只展示中断态。
-->
<script setup lang="ts">
import { GRAPH_NODES, NODE_LABELS, useSessionStore } from "@/stores/session";

const store = useSessionStore();
</script>

<template>
  <footer class="progress-bar">
    <div class="steps">
      <template v-for="(node, i) in GRAPH_NODES" :key="node">
        <span v-if="i > 0" class="arrow">→</span>
        <span class="step" :class="`step-${store.progress[node]}`">
          <span class="mark">{{ store.progress[node] === "done" ? "✓" : store.progress[node] === "running" ? "◌" : "·" }}</span>
          {{ NODE_LABELS[node] }}
        </span>
      </template>
    </div>
    <span v-if="store.busy" class="status busy">执行中…</span>
    <span v-else-if="store.question" class="status interrupted">已中断，等待补充</span>
    <span v-else-if="store.sessionId" class="status idle">空闲</span>
  </footer>
</template>

<style scoped>
.progress-bar {
  display: flex; align-items: center; gap: 12px;
  border-top: 1px solid #d0d7de; background: #fff;
  padding: 4px 12px; font-size: 12px; min-height: 30px;
}
.steps { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.arrow { color: #8b949e; }
.step { color: #8b949e; display: inline-flex; gap: 3px; align-items: center; }
.step-running { color: #1677ff; font-weight: 600; }
.step-done { color: #1a7f37; }
.mark { font-family: Consolas, monospace; }
.status { margin-left: auto; }
.status.busy { color: #1677ff; }
.status.interrupted { color: #9a6700; }
.status.idle { color: #8b949e; }
</style>
