/**
 * 前端入口（架构 §9.4 正式版：Vue3 + Vite + TS + Pinia）。
 * store.init() 在 Workbench 挂载时执行：建会话 → 连 SSE。
 */
import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "./App.vue";
import "./style.css";

createApp(App).use(createPinia()).mount("#app");
