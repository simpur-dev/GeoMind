# GeoMind 空间智能体

> 2026 OceanBase 数据库大赛 · 高德赛道（AI + 数据库应用开发方向）参赛方案
> 当前阶段：**阶段 0（赛题发布前 · 地基）** ｜ 架构设计已定稿，代码零启动

---

## 一句话定位

**同库五模，一条 SQL。** 在 OceanBase SeekDB 单引擎内，一行记录同时承载「标量属性 + POINT/POLYGON 空间列 + VECTOR 语义向量列 + ngram 全文 + JSON」，用一条 SQL 完成「空间圈选 + 业务条件 + 语义召回」的混合检索，并把空间计算**下推数据库内核**——而不是把坐标搬到 Python 里算。

## 三条差异化叙事

| # | 叙事 | 说明 |
|---|---|---|
| 1 | **空间计算下推内核** | 距离 / 包含 / 缓冲区 / 圈选交给 SeekDB 空间函数与 SPATIAL INDEX，LLM 绝不参与坐标计算 |
| 2 | **单库五模混合检索** | 标量（B-tree）+ 空间（SPATIAL）+ 语义（cosine HNSW）+ 全文（ngram）+ 半结构（JSON），一条 SQL 出结果，向量侧走 `VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)` |
| 3 | **Spatial State 闭环** | 对话产物是**持续更新的结构化空间状态**，不是一次性文本报告；用户圈选 / 拖拽 / 改约束触发依赖图驱动的局部重算 |

形态上除 Web 应用外，空间能力同时封装为 **MCP Server（Streamable HTTP）**，可直接被第三方智能体调用。

## 当前状态

阶段 0 共 8 项验收：**8 / 8 全部完成**。最终验收锚点「多模混合检索最小闭环」已点亮（一条 SQL 四模 24.9 ms）；Map Spec 渲染器 + 前端三栏工作台已落地并经浏览器端到端实测（高德底图 / 无 key 降级双档，对话→地图→框选回传闭环全通）。

| # | 验收项 | 状态 |
|---|---|---|
| 1 | Docker 版 SeekDB V1.4.0 + 五模复验 + V1.3 回滚路径 | ✅ 完成 |
| 2 | 能力实测（SPATIAL INDEX / cosine HNSW / 三模 EXPLAIN）+ 10 万级压测 | ✅ 完成 |
| 3 | 落地生产 DDL + 灌入城市样例数据 | ✅ 完成（`geomind` 库 6 表 + 2 万 POI） |
| 4 | Skill 基类 + Registry + MCP Server 骨架 | ✅ 完成（9 工具，tools/list 可见） |
| 5 | Spatial State + LangGraph 7 步骨架 | ✅ 完成（含 interrupt 追问与恢复） |
| 6 | Map Spec 渲染器 | ✅ 完成（kind 注册表 + 增量 diff + 高德/降级画布双档适配器） |
| 7 | 前端三栏工作台 + SSE | ✅ 完成（Vue3+Vite+TS+Pinia；SSE 真流 + Last-Event-ID 续传；浏览器实测） |
| 8 | **多模混合检索最小闭环（验收锚点）** | ✅ 完成（四模 24.9 ms，含可证伪对照） |

完整清单、风险账与优先级见 [`docs/开发进度.md`](docs/开发进度.md)。

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/GeoMind架构设计文档.md`](docs/GeoMind架构设计文档.md) | **权威基线 v1.4**：六层架构、四条铁律、Spatial State Schema、Skill/MCP 插件机制、存储设计、前后端契约、路线图 |
| [`docs/SeekDB环境实测报告.md`](docs/SeekDB环境实测报告.md) | 实跑结论（**与架构文档冲突时以本报告为准**）：能力矩阵、算子白名单、EXPLAIN 证据、V1.4 迁移记录 |
| [`docs/开发进度.md`](docs/开发进度.md) | 阶段 0 checklist、环境坐标速查、时间线、风险表、下一步优先级 |
| [`docs/设计方案-技能层与状态层骨架.md`](docs/设计方案-技能层与状态层骨架.md) | 应用骨架设计（8 模块 / 不做清单 / 实施步骤 / 测试矩阵） |
| [`backend/`](backend/) | 后端：Query Builder（三层卡点）+ L4 计算路由 SpatialRouter + 9 Skill + MCP + REST/SSE + LangGraph 7 步 + CLI。123 个 pytest + ruff + mypy 全绿 |
| [`docs/调研报告-优秀案例.md`](docs/调研报告-优秀案例.md) | 60+ 来源汇编：学术 9 例、工业 5 例、框架选型结论 |
| [`docs/architecture.html`](docs/architecture.html) | 六层架构可视化大图 |
| [`docs/压测报告-10万级.md`](docs/压测报告-10万级.md) | 10 万级索引压测：空间索引不命中的坐实与两个绕行方案的量化 |
| [`docs/sql/verify_v140.sql`](docs/sql/verify_v140.sql) | 五模 + 三模混合 7 项复验脚本，一键重跑 |
| [`backend/db/migrations/001_init_schema.sql`](backend/db/migrations/001_init_schema.sql) | **生产 DDL 权威源**（6 表，含 `lon/lat/grid_id` 冗余列与三类索引） |
| [`backend/etl/`](backend/etl/) | 样例数据生成器 `seed_cd.py` + 验证脚本生成器 `gen_verify.py`（四模闭环 / 空间连接 / 状态读写） |
| [`docs/research/`](docs/research/) | 调研底稿：学术智能体 / 工业框架 / OceanBase / 高德 PDF 精读 |

## 环境坐标

```powershell
# 连库（MySQL 客户端已在 D 盘）
& "D:\mysql\mysql-8.4.8-winx64\bin\mysql.exe" -h 127.0.0.1 -P 2881 -uroot --ssl-mode=DISABLED

# 容器运维
docker start|stop|restart seekdb

# 五模 + 三模复验（7 项）
Get-Content "docs\sql\verify_v140.sql" -Raw |
  & "D:\mysql\mysql-8.4.8-winx64\bin\mysql.exe" -h 127.0.0.1 -P 2881 -uroot --ssl-mode=DISABLED --table

# 生产库：建表（权威 DDL）
& "D:\mysql\mysql-8.4.8-winx64\bin\mysql.exe" -h 127.0.0.1 -P 2881 -uroot --ssl-mode=DISABLED --force `
  -e "source E:/2026ob_projects/GeoMind/backend/db/migrations/001_init_schema.sql"

# 生产库：生成样例数据 → 生成验证脚本 → 跑四模闭环
$py = "C:\Users\34928\.workbuddy\binaries\python\versions\3.13.12\python.exe"
& $py backend\etl\seed_cd.py; & $py backend\etl\gen_verify.py
& "D:\mysql\mysql-8.4.8-winx64\bin\mysql.exe" -h 127.0.0.1 -P 2881 -uroot --ssl-mode=DISABLED --table --force `
  -e "source E:/2026ob_projects/GeoMind/backend/etl/data/verify_prod.sql"
```

| 项 | 值 |
|---|---|
| 数据库 | Docker 容器 `seekdb`，镜像 `quay.io/oceanbase/seekdb:latest`，`-p 2881:2881`，named volume `seekdb_data` |
| 内核 | `5.7.25-OceanBase seekdb-v1.4.0.0` / OceanBase 4.3.5.3 (r1.4.0.0) |
| 连接 | 127.0.0.1:2881，root，**空密码**，`--ssl-mode=DISABLED` |

### 写 SQL 前必看的三条硬约束

1. **DDL 一律 SRID 0** —— SRID 4326 列级声明报 ERROR 3548，`CREATE SPATIAL REFERENCE SYSTEM` 报 ERROR 1064（不支持），两条路都堵死；坐标按 WGS84 经纬度存值。
2. **单位分家** —— `ST_Distance_Sphere` 返回**米**；`ST_Distance` / `_ST_Dwithin` / `ST_Buffer` 参数是**度**。
3. **禁写分布式选项** —— 不得出现 `REPLICA_NUM`（库级/表级均 ERROR 1064），也不要 `ORGANIZATION INDEX / TABLET_SIZE / USE_BLOOM_FILTER / BLOCK_SIZE`；向量索引 `LIB=VSAG` 省略。
4. **🔴 空间索引是"装饰品"** —— `SPATIAL KEY` 在 V1.4 **完全不参与查询计划**（7 种谓词 + `INDEX` hint 全部 `TABLE FULL SCAN`，`MBR*` 函数不存在）。空间查询必须走「`lon/lat` 冗余列 + `KEY(lon,lat)`」MBR 预筛再用 `ST_*` 精确复核；空间连接走「`grid_id` 冗余列 + 普通索引」。见下节量化数据。

### 空间算子边界（实测）

| 类别 | 函数 |
|---|---|
| ✅ 可下推 | `ST_Distance_Sphere`、`ST_Distance`、`_ST_Dwithin`、`ST_Intersects`、`ST_Contains`、`ST_Within`、`ST_Buffer`、`ST_Centroid`、`ST_Area` |
| ❌ 不可下推 | `ST_Intersection`、`ST_Union_Agg`（ERROR 1305）→ 交并运算走应用层 Shapely 或高德 |
| ⚠️ 功能可、性能不可 | 空间连接 `JOIN ON ST_Contains + GROUP BY` 结果正确，但 EXPLAIN = `NESTED-LOOP JOIN` + 双侧 `TABLE FULL SCAN`（不走空间索引） |

### 绕行方案实测收益（不依赖 SPATIAL INDEX，计算仍在库内）

| 场景 | 原方案 | 绕行方案 | 收益 |
|---|---|---|---|
| 圈选（1.5 km） | 15.7 ms 全扫 | `lon/lat` + `KEY(lon,lat)` MBR 预筛：**4.6 ms** | 3.4×（10 万级实测 >60×） |
| KNN top10 | 19.4 ms | MBR 预筛后排序：**11.8 ms**（结果逐行一致） | 1.6×（10 万级 7×） |
| 网格聚合（25 网格 × 2 万点） | `ST_Contains` **274.5 ms** | `grid_id` 冗余列 JOIN：**2.2 ms** | **127×**（10 万级 278×） |
| 向量 ANN（10 万 × 1024 维） | 精确 123 ms | HNSW 近似 **61 ms**，召回 10/10 | 2× |

**四模混合（生产库 2 万 POI）**：全文(ngram AND) + 向量(cosine ANN) + 空间(MBR + ST) + 标量 → **24.9 ms**，EXPLAIN = `VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)`，全文谓词亦进入预过滤（`pushdown_match_filter`）。对照组（纯向量无过滤）TOP10 仅 1 条落在圈内，证明多模过滤在索引扫描阶段生效。

**三通道平权**（架构 §9.1）：CLI（`python -m backend.demo "查询"`）、REST/SSE（`python -m backend.main`，127.0.0.1:8000）、MCP（`python -m backend.mcp_server.server`，127.0.0.1:8765/mcp）挂在同一套内核契约上，前端可整体摘除。

**质量门**：`pytest` 123 个用例（QueryBuilder 卡点 / Skill 对拍 / 空间计算路由与降级链 / 状态局部重算 / 7 步图 / MCP tools/list）+ `ruff check` + `mypy` 全绿；关键断言另做变异检查逐条验红。Python 3.11 隔离 venv：`D:\python_env\geomind`。

## 路线图

| 阶段 | 内容 |
|---|---|
| 阶段 0（当前） | 地基：容器环境、能力实测、生产 DDL、Skill/MCP 骨架、Spatial State + LangGraph、Map Spec、前端、点亮多模混合检索最小闭环 |
| 阶段 1 | 任务书发布后按场景参数化 Skill、补赛题数据、对齐官方评分口径 |
| 阶段 2 | 依赖图局部重算全场景化、混合查询压测、反馈记忆回流、答辩演示脚本 |

## 备注

- 仓库中的 `docs/高德开放平台整体介绍+AI+空间智能应用 202608.pdf` 为赛题方提供的官方参考资料，仅供本参赛项目内部查阅，其见解已提炼为 [`docs/research/04-gaode-pdf.md`](docs/research/04-gaode-pdf.md)。
- 本仓库尚未选定开源许可证（赛题进行中，暂不声明）。
