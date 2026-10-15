# GeoMind Web 工作台

轻量可摘除的演示壳（架构 §9）：Vue3 + Vite + TypeScript + Pinia，只消费后端契约
（REST + SSE + Map Spec + GeoJSON），**不含任何业务判断**——删掉整个 `web/` 目录，
内核（CLI / REST / MCP）零影响（§9.8 可摘除性验收）。

## 快速开始

```bash
# 1. 启动后端（8000 端口，依赖 OceanBase 容器）
uvicorn backend.app:app --port 8000

# 2. 启动前端 dev server（8001，/api 已代理到 8000，含 SSE 流式转发）
npm install
npm run dev
# 打开 http://localhost:8001
```

## 地图底图：高德 key（可选，不配也能用）

本仓库**不内置任何有效 key**（合规硬规则）。未配置 key 时前端自动进入
**无底图降级模式**：内置 SVG 画布渲染同一份 Map Spec（网格 + POI 点 + 热力方块），
hello-world 与框选回传不依赖 key 即可验收；配置 key 后自动升级高德底图，
渲染器代码路径不变（两档适配器实现同一 MapAdapter 接口，§9.4）。

申请入口（免费）：

1. 前往 [高德开放平台](https://lbs.amap.com/) → 控制台 → 应用管理 → 创建应用；
2. 为应用添加 Key，平台选 **「Web端(JS API)」**；
3. 复制 `web/.env.example` 为 `web/.env`，填入：
   ```
   VITE_AMAP_KEY=<你的 key>
   # 2021-12-02 之后申请的 key 需要配套安全密钥（与 key 同页创建）：
   VITE_AMAP_SECURITY_JSCODE=<你的安全密钥>
   ```
4. 在控制台为该 key 配置 **域名白名单**（前端 key 是公开的，靠 Referer 白名单防盗用；
   生产环境应由后端签名服务下发，见架构 §9.7）。

## 质量门

```bash
npm run typecheck   # vue-tsc --noEmit
npm run build       # vue-tsc + vite build
```

## 目录结构（架构 §9.5）

```
web/src/
├── api/          # REST client + SSE client + Map Spec 类型与增量 diff
├── layers/       # 按 Map Spec kind 注册的渲染器（registry + amap/fallback 两档适配器）
├── stores/       # Pinia 会话状态（仅视图缓存，非权威）
├── views/        # Workbench 三栏工作台
└── components/   # ChatPanel / MapCanvas / ResultPanel / ProgressBar
```

## 交互说明

| 操作 | 降级模式 | 高德模式 |
|---|---|---|
| 框选（draw_polygon 回传） | Shift + 拖拽 | 工具条「框选」按钮后拖拽 |
| 点选 POI（point_select 回传） | 点圆点 | 点 marker |
| 图层开关 | 地图左下角工具条 | 同左 |
| 列表 ↔ 地图联动 | 点右栏候选行高亮地图点位 | 同左 |

坐标约定：前后端之间全部 **GCJ-02**（高德底图坐标系），WGS84 → GCJ-02 转换在
后端 `backend/api/geo.py` 完成，前端零计算（§9.6 职责边界表）。
