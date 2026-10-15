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

阶段 0 共 8 项验收：**1 项完成 · 1 项部分完成 · 6 项未开始**（含最终验收锚点「多模混合检索最小闭环」）。

| # | 验收项 | 状态 |
|---|---|---|
| 1 | Docker 版 SeekDB V1.4.0 + 五模复验 + V1.3 回滚路径 | ✅ 完成 |
| 2 | 能力实测（SPATIAL INDEX / cosine HNSW / 三模 EXPLAIN） | 🟡 部分（10 万级索引命中未验证） |
| 3 | 落地生产 DDL + 灌入城市样例数据 | ❌ 未开始 |
| 4 | Skill 基类 + Registry + MCP Server 骨架 | ❌ 未开始 |
| 5 | Spatial State + LangGraph 7 步骨架 | ❌ 未开始 |
| 6 | Map Spec 渲染器 | ❌ 未开始 |
| 7 | 前端三栏工作台 + SSE | ❌ 未开始 |
| 8 | **多模混合检索最小闭环（验收锚点）** | ❌ 未开始 |

完整清单、风险账与优先级见 [`docs/开发进度.md`](docs/开发进度.md)。

## 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/GeoMind架构设计文档.md`](docs/GeoMind架构设计文档.md) | **权威基线 v1.3**：六层架构、四条铁律、Spatial State Schema、Skill/MCP 插件机制、存储设计、前后端契约、路线图 |
| [`docs/SeekDB环境实测报告.md`](docs/SeekDB环境实测报告.md) | 实跑结论（**与架构文档冲突时以本报告为准**）：能力矩阵、算子白名单、EXPLAIN 证据、V1.4 迁移记录 |
| [`docs/开发进度.md`](docs/开发进度.md) | 阶段 0 checklist、环境坐标速查、时间线、风险表、下一步优先级 |
| [`docs/调研报告-优秀案例.md`](docs/调研报告-优秀案例.md) | 60+ 来源汇编：学术 9 例、工业 5 例、框架选型结论 |
| [`docs/architecture.html`](docs/architecture.html) | 六层架构可视化大图 |
| [`docs/sql/verify_v140.sql`](docs/sql/verify_v140.sql) | 五模 + 三模混合 7 项复验脚本，一键重跑 |
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

### 空间算子边界（实测）

| 类别 | 函数 |
|---|---|
| ✅ 可下推 | `ST_Distance_Sphere`、`ST_Distance`、`_ST_Dwithin`、`ST_Intersects`、`ST_Contains`、`ST_Within`、`ST_Buffer`、`ST_Centroid`、`ST_Area` |
| ❌ 不可下推 | `ST_Intersection`、`ST_Union_Agg`（ERROR 1305）→ 交并运算走应用层 Shapely 或高德 |
| ⚠️ 功能可、性能不可 | 空间连接 `JOIN ON ST_Contains + GROUP BY` 结果正确，但 EXPLAIN = `NESTED-LOOP JOIN` + 双侧 `TABLE FULL SCAN`（不走空间索引） |

## 路线图

| 阶段 | 内容 |
|---|---|
| 阶段 0（当前） | 地基：容器环境、能力实测、生产 DDL、Skill/MCP 骨架、Spatial State + LangGraph、Map Spec、前端、点亮多模混合检索最小闭环 |
| 阶段 1 | 任务书发布后按场景参数化 Skill、补赛题数据、对齐官方评分口径 |
| 阶段 2 | 依赖图局部重算全场景化、混合查询压测、反馈记忆回流、答辩演示脚本 |

## 备注

- 仓库中的 `docs/高德开放平台整体介绍+AI+空间智能应用 202608.pdf` 为赛题方提供的官方参考资料，仅供本参赛项目内部查阅，其见解已提炼为 [`docs/research/04-gaode-pdf.md`](docs/research/04-gaode-pdf.md)。
- 本仓库尚未选定开源许可证（赛题进行中，暂不声明）。
