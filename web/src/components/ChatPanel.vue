<!--
左栏（架构 §9.2，~22%）：多轮消息流 + 约束 chips + 输入框 + HITL 追问流。
工具调用可观测：SSE tool.start/end 已由 store 落为 role=tool 的消息行。
约束 chips 只读展示（constraint_edit 回传端点在后端路线图内，暂不开放编辑）。
-->
<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import { NODE_LABELS, useSessionStore } from "@/stores/session";

const store = useSessionStore();
const input = ref("");
const listEl = ref<HTMLElement | null>(null);

const chips = (): string[] => {
  const c = store.constraints;
  if (!c) return [];
  const out: string[] = [];
  if (c.center) out.push(`中心 ${(c.center as number[]).map((n) => n.toFixed(4)).join(",")}`);
  if (c.radius_m) out.push(`半径 ${c.radius_m}m`);
  if (c.category_id) out.push(`类目 ${c.category_id}`);
  if (c.min_rating) out.push(`评分≥${c.min_rating}`);
  if (c.time_bucket) out.push(`时段 ${c.time_bucket}`);
  for (const k of (c.keywords as string[]) ?? []) out.push(`关键词 ${k}`);
  return out;
};

/**
 * HITL 追问文案。类型断言放在 script 里而不是模板里：模板表达式
 * `{{ (store.question as { question?: string }).question }}` 会让 vue-tsc 的
 * 模板解析器在可选属性 `?` 处崩（TS1109/TS1128/TS1434，实测 4 条）。
 */
const questionText = computed(() => {
  const q = store.question as { question?: string } | null;
  return q?.question ?? "";
});

watch(() => store.messages.length, async () => {
  await nextTick();
  listEl.value?.scrollTo({ top: listEl.value.scrollHeight });
});

function send(): void {
  const q = input.value.trim();
  if (!q || store.busy) return;
  input.value = "";
  void store.ask(q);
}

function resume(): void {
  const a = input.value.trim();
  if (!a || store.busy) return;
  input.value = "";
  void store.resume(a);
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === "Enter" && !e.isComposing) (store.question ? resume() : send());
}
</script>

<template>
  <aside class="chat-panel">
    <div ref="listEl" class="msg-list">
      <div v-for="m in store.messages" :key="m.id" class="msg" :class="`msg-${m.role}`">
        <template v-if="m.role === 'tool'">{{ m.text }}</template>
        <template v-else>{{ m.role === "user" ? "我" : "GeoMind" }}：{{ m.text }}</template>
      </div>

      <div v-if="store.question" class="interrupt">
        <div class="interrupt-title">⏸ 需要补充信息</div>
        <div class="interrupt-q">{{ questionText }}</div>
        <div class="interrupt-hint">在下方输入补充内容后回车，将从断点继续执行。</div>
      </div>
    </div>

    <div v-if="chips().length" class="chips">
      <span v-for="c in chips()" :key="c" class="chip">{{ c }}</span>
    </div>

    <div class="input-row">
      <input
        v-model="input"
        class="chat-input"
        :placeholder="store.question ? '补充信息…' : '例如：帮我在春熙路附近选个咖啡馆位置'"
        :disabled="store.busy"
        @keydown="onKeydown"
      >
      <button class="send-btn" :disabled="store.busy || !input.trim()" @click="store.question ? resume() : send()">
        发送
      </button>
    </div>
    <div class="hint">意图低置信度时会中断追问（LangGraph interrupt）· 节点：{{ Object.keys(NODE_LABELS).length }} 步</div>
  </aside>
</template>

<style scoped>
.chat-panel {
  display: flex; flex-direction: column; height: 100%;
  border-right: 1px solid #d0d7de; background: #fff; min-width: 0;
}
.msg-list { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 8px; }

.msg { font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }
.msg-user { align-self: flex-end; background: #e7f1ff; border-radius: 8px 8px 2px 8px; padding: 6px 10px; max-width: 85%; }
.msg-assistant { align-self: flex-start; background: #f6f8fa; border-radius: 8px 8px 8px 2px; padding: 6px 10px; max-width: 92%; }
.msg-tool { align-self: stretch; color: #7d8590; font-size: 12px; font-family: Consolas, monospace; }

.interrupt {
  border: 1px solid #d4a72c66; background: #fff8c5; border-radius: 8px;
  padding: 8px 10px; font-size: 13px;
}
.interrupt-title { font-weight: 600; color: #7d4e00; margin-bottom: 4px; }
.interrupt-q { color: #1f2328; }
.interrupt-hint { color: #7d8590; font-size: 12px; margin-top: 4px; }

.chips { display: flex; flex-wrap: wrap; gap: 4px; padding: 6px 10px; border-top: 1px solid #eaeef2; }
.chip {
  font-size: 11px; background: #f6f8fa; border: 1px solid #d0d7de;
  border-radius: 10px; padding: 1px 8px; color: #57606a;
}

.input-row { display: flex; gap: 6px; padding: 8px 10px 4px; }
.chat-input {
  flex: 1; border: 1px solid #d0d7de; border-radius: 6px; padding: 6px 8px;
  font-size: 13px; outline: none; min-width: 0;
}
.chat-input:focus { border-color: #1677ff; }
.send-btn {
  border: none; background: #1677ff; color: #fff; border-radius: 6px;
  padding: 6px 14px; font-size: 13px; cursor: pointer;
}
.send-btn:disabled { background: #aecbfa; cursor: not-allowed; }
.hint { padding: 0 10px 8px; font-size: 11px; color: #8b949e; }
</style>
