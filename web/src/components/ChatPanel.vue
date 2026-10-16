<!--
左栏（架构 §9.2，~22%）：多轮消息流 + 约束 chips + 输入框 + HITL 追问流。
工具调用可观测：SSE tool.start/end 已由 store 落为 role=tool 的消息行。
约束 chips：白名单四项（半径 / 评分 / 等时圈 / 方式）可点改——点一下原地变输入控件，
回车提交、Esc 或失焦取消；中心 / 类目 / 时段 / 关键词只读（§9.2 + 卡 6 设计决定 1）。
前端不判合法性（§9.6）：值原样上报，越界由后端 422，错误文案显示在 chips 下方。
-->
<script setup lang="ts">
import { computed, nextTick, ref, watch } from "vue";

import { NODE_LABELS, useSessionStore } from "@/stores/session";

const store = useSessionStore();
const input = ref("");
const listEl = ref<HTMLElement | null>(null);

/** 可点改的四个键（白名单的唯一出处在后端 `ConstraintOverrides`，这里只是控件形态表）。 */
type EditKey = "radius_m" | "min_rating" | "iso_minutes" | "travel_mode";

interface Chip {
  /** null = 只读 chip（中心/类目/时段/关键词）。 */
  key: EditKey | null;
  label: string;
  value: unknown;
}

const has = (v: unknown): boolean => v !== null && v !== undefined;

const chips = computed<Chip[]>(() => {
  const c = store.constraints;
  if (!c) return [];
  const out: Chip[] = [];
  if (c.center) out.push({ key: null, label: `中心 ${(c.center as number[]).map((n) => n.toFixed(4)).join(",")}`, value: c.center });
  if (has(c.radius_m)) out.push({ key: "radius_m", label: `半径 ${String(c.radius_m)}m`, value: c.radius_m });
  if (c.category_id) out.push({ key: null, label: `类目 ${String(c.category_id)}`, value: c.category_id });
  if (has(c.min_rating)) out.push({ key: "min_rating", label: `评分≥${String(c.min_rating)}`, value: c.min_rating });
  if (c.time_bucket) out.push({ key: null, label: `时段 ${String(c.time_bucket)}`, value: c.time_bucket });
  if (has(c.iso_minutes)) out.push({ key: "iso_minutes", label: `等时圈 ${String(c.iso_minutes)}min`, value: c.iso_minutes });
  if (has(c.travel_mode)) out.push({ key: "travel_mode", label: `方式 ${c.travel_mode === "walking" ? "步行" : "驾车"}`, value: c.travel_mode });
  for (const k of (c.keywords as string[]) ?? []) out.push({ key: null, label: `关键词 ${k}`, value: k });
  return out;
});

const editingKey = ref<EditKey | null>(null);
// `v-model` 在 `type=number` 上会自动把值转成 number（Vue 的 looseNum），
// 所以这里必须按 string|number 两种形态收，不能假设一定是字符串。
const draft = ref<string | number>("");
const chipsEl = ref<HTMLElement | null>(null);

function startEdit(chip: Chip): void {
  if (!chip.key || store.busy) return;
  if (chip.key === "travel_mode") {
    // 两态切换：只有 walking / driving 两个取值（高德无骑行档），点一下即互换并提交
    void store.editConstraint({ travel_mode: chip.value === "walking" ? "driving" : "walking" });
    return;
  }
  editingKey.value = chip.key;
  draft.value = String(chip.value ?? "");
  void nextTick(() => chipsEl.value?.querySelector("input")?.focus());
}

function submitEdit(): void {
  const key = editingKey.value;
  editingKey.value = null;
  if (!key) return;
  const raw = String(draft.value ?? "").trim();
  // 空串/组不成数时不发请求：这是「没有可发送的值」，不是前端裁决合法性
  // （区间、枚举一律不判——`type=number` 不设 min/max，越界由后端 422，§9.6）
  const num = Number(raw);
  if (raw && Number.isFinite(num)) void store.editConstraint({ [key]: num });
}

function cancelEdit(): void {
  editingKey.value = null;
}

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

    <div v-if="chips.length" ref="chipsEl" class="chips">
      <template v-for="c in chips" :key="c.label">
        <input
          v-if="c.key && c.key === editingKey"
          v-model="draft"
          class="chip-input"
          type="number"
          :step="c.key === 'min_rating' ? 0.5 : 1"
          @keydown.enter="submitEdit"
          @keydown.esc="cancelEdit"
          @blur="cancelEdit"
        >
        <span
          v-else
          class="chip"
          :class="{ 'chip-edit': c.key }"
          :title="c.key ? '点一下改这个约束（回车提交，Esc 取消）' : ''"
          @click="startEdit(c)"
        >{{ c.label }}<i v-if="c.key" class="chip-pen">✎</i></span>
      </template>
    </div>
    <div v-if="store.error" class="chip-error">{{ store.error }}</div>

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
.chip-edit { cursor: pointer; border-color: #aecbfa; background: #e7f1ff; color: #0b4ea2; }
.chip-edit:hover { border-color: #1677ff; }
.chip-pen { font-style: normal; margin-left: 3px; opacity: 0.6; }
.chip-input {
  width: 74px; font-size: 11px; border: 1px solid #1677ff; border-radius: 10px;
  padding: 1px 6px; color: #0b4ea2; background: #fff; outline: none;
}
.chip-error { padding: 0 10px 6px; font-size: 11px; color: #cf222e; }

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
