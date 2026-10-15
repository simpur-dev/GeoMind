# GeoMind 架构骨架（Organizer 设计基线 v0.1）

> 本文件是架构设计的模式基线，供撰写《GeoMind架构设计文档.md》使用。调研结论回来后，DDL/SQL 与技术选型以调研核实结果为准。

## 0. 设计主线（一句话）
以**结构化空间状态（Spatial State）**为核心产物，用 LangGraph 状态图驱动高德"7 步闭环"，把空间计算与多模检索作为**确定性 Skill 工具**下沉到 OceanBase 与高德 API，LLM 只负责意图理解、约束建模、规划与结果解读；场景能力以 MCP Skill 插件化挂载，核心零改动扩展新场景。

## 1. 四条铁律（继承已讨论的设计原则）
1. 空间计算绝不下放给 LLM：距离/包含/路径/可达性/聚合一律由 OceanBase 空间函数或高德 API 完成，LLM 不做坐标运算。
2. 状态结构化、增量更新：对话产物是 Spatial State（JSON），不是自由文本；每次用户修改只重算受影响的节点（依赖图驱动局部重算）。
3. 数据三层抽象后喂 LLM：原始事实（POI/客流/轨迹）→ 空间抽象（候选集/覆盖/可达域）→ 决策特征（评分/排名/标签），LLM 只消费决策特征与聚合摘要。
4. 交互闭环：地图是一等输出与输入（圈选/拖拽/改约束），渲染方案（Map Spec）也是状态的一部分，反馈触发回到第 1/2 步。

## 2. 六层架构（在原 5 层设想上演进，不推倒重来）

### L1 交互感知与空间表达层（Interaction & Spatial Expression）
- 多模态输入：自然语言对话；地图交互（点选、圆圈/多边形圈选、拖拽改范围、时间轴）；表单参数。
- 意图前置解析：把地图手势翻译为结构化空间约束（center/radius/polygon），与文本一起进入大脑。
- 空间表达（对应高德"空间表达"三要素）：
  - 几何表达：点/线/面/热力/可达圈/路径（高德 JS API + Loca 图层）
  - 数据表达：卡片、排名表、指标面板、图表
  - 交互表达：图层开关、约束编辑、对比、追问
- 输出渲染由 Map Spec（声明式 JSON）驱动，前端只负责渲染。

### L2 空间大脑层（Spatial Brain / Agent Orchestration）
- **Intent Interpreter 意图理解器**：NL+手势 → 场景识别 + 槽位填充（场景路由到 Skill）。
- **Constraint Builder 约束建模器**：生成/维护三类约束——空间约束（几何）、标量约束（业务字段）、语义约束（自然语言→embedding/查询改写），外加时间约束。
- **Planner 规划器**：Plan-and-Execute 为主（先生成 DAG 计划，再逐步执行确定性工具），ReAct 作为兜底处理歧义/失败。
- **State Manager 状态管理器**：Spatial State 读写、版本号、依赖图、局部重算调度（DAG 节点失效标记）、检查点。
- **Decision Synthesizer 决策综合器**：把决策特征组织成结论/对比/理由（数据决策→地图表达之间）。
- **LLM 网关**：模型可插拔（豆包/DeepSeek/本地），结构化输出（Pydantic/JSON Schema 约束）。
- 实现载体：LangGraph StateGraph（节点=7步，边=条件路由），checkpointer 持久化。

### L3 空间技能层（Spatial Skills / MCP 插件层）
- 两层技能：
  - **原子能力 Skill（Atomic Skills）**：地理编码、周边/多边形检索、多模混合检索、路径规划、可达性分析、客流聚合、人群画像、评分排名、选址打分、轨迹分析……
  - **场景 Skill（Scene Skills）**：智能选址、文旅行程助手、门店运营、客流洞察、营销触达——由原子 Skill 编排而成（DAG 模板）。
- 每个 Skill 实现统一基类 SpatialSkill，向 MCP Server 注册为 tools；Skill Registry 支持装饰器/entry-point 自动发现。
- 新场景 = 新增一个 Skill 包 + （可选）新表/新图层配置，核心代码零改动。

### L4 空间数据与计算服务层（Spatial Data & Compute Services）
- **多模检索服务 Multi-Modal Retrieval Service**：唯一出口，把三类约束编译成一条 SQL（空间 ST_* 过滤 + 标量 WHERE + 向量 ANN 排序），下推 OceanBase。
- **空间计算服务 Spatial Compute Service**：计算路由——能下推的（距离/包含/连接/聚合/缓冲区）走 OB 函数；OB 不支持的（路径规划、实时路况、行政边界、逆地理编码）走高德 API；结果归一化。
- **数据加工服务 Feature Pipeline**：原始事实 → 空间抽象 → 决策特征（离线 ETL + 在线特征计算），embedding 生成。
- **高德适配网关 Amap Adapter**：统一封装高德 key、限流、缓存、字段归一（搜索/路径/猎鹰/交通/天气/地理编码）。
- **Map Spec 生成器**：把状态中的图层/结果转成声明式渲染 JSON。

### L5 状态与记忆层（State & Memory）
- 会话状态库：Spatial State（JSON 列 + 版本），存 OceanBase 关系表（或 Redis 热缓存 + OB 持久化）。
- 检查点：LangGraph checkpointer（同库）。
- 长期记忆：领域知识、用户偏好、历史方案/案例——向量化后仍存 OceanBase（VECTOR 列），复用多模检索，不引第二套向量库。
- 反馈记忆：用户采纳/否决的候选与理由，回流为偏好与评估样本。

### L6 基础设施层（Infrastructure）
- OceanBase 4.x（MySQL 模式）：关系 + SDO_GEOMETRY + VECTOR 一体；空间索引（R-Tree）、向量索引（HNSW）。
- 数据管线：POI/门店/客流/轨迹/竞品数据导入（批处理脚本、高德 API 采集器）。
- Embedding 模型服务（本地 bge-m3 或 API）；LLM API；高德开放平台。
- 部署：Docker Compose（observer + backend + frontend + mcp-server + 数据管线），可迁移 OCP/云。

## 3. 七层到 7 步闭环的映射
- 理解意图 → L2 Intent Interpreter
- 建立约束 → L2 Constraint Builder（输入含 L1 手势）
- 生成候选 → L3 Skill 调 L4 多模检索（空间+标量+向量）
- 空间计算 → L4 路由到 OB 空间函数 / 高德 API
- 数据决策 → L4 特征加工 + L2 Decision Synthesizer
- 地图表达 → L4 Map Spec → L1 高德 JS/Loca
- 用户反馈 → L1 采集 → L2 State Manager 局部重算（回到 1/2/4）

## 4. Spatial State Schema（字段级草案，文档中给完整 JSON 示例）
顶层字段：
- session_id, scene（场景 Skill 名）, version, updated_at
- intent: {raw, type, slots, confidence}
- constraints: {spatial[], scalar[], semantic[]{text, embedding_ref}, temporal[]}
- plan: {steps[{id, skill, inputs, status, depends_on[]}]}（= 可执行 DAG / 局部重算依赖图）
- candidates: [{id, type, name, location, attrs, scores{}, rank, reasons[]}]
- layers: [{id, kind(heatmap/isochrone/poi/route/polygon), data_ref, style, status, derived_from[]}]
- decision: {selected_id, rationale, metrics{}, alternatives[]}
- map_view: {center, zoom, pitch, layer_order[]}
- memory_refs: {knowledge[], preferences[]}
- feedback: [{round, action, target, payload, t}]
- history: 事件/版本快照（或仅存 diff）

关键机制：plan.steps 与 layers/candidates 带 derived_from 依赖；用户修改约束 c1 时，只把依赖 c1 的节点标记 stale 并重算，其余复用。

## 5. Skill 插件机制（接口规范要点）
- Python 基类 SpatialSkill（ABC）：
  - metadata: name/version/scene/description/intent_examples（用于场景路由）
  - input_schema / output_schema（Pydantic）
  - required_atomic_tools: 声明依赖的原子能力
  - build_plan(state) -> Plan（DAG 模板，节点引用原子工具）
  - synthesize(state) -> decision（LLM 解读，仅消费决策特征）
  - render_spec(state) -> MapSpec
- 注册：@register_skill 装饰器 + 包入口扫描 skills/ 目录；启动时 Skill Registry 汇总，自动暴露为 MCP tools（list_tools/call_tool）。
- 场景 Skill 只写"编排 + 评分口径 + 渲染模板"，不写数据访问（数据访问全在 L4）。
- 扩展契约：新增场景不改 L2/L4 代码；允许新增表/视图（通过 migration 目录）与新图层样式。
- 同时作为 MCP Server 交付：可被外部 Agent（如高德 Agent 形态/评委环境）直接调用，契合高德"Skill/MCP Server"交付形态。

## 6. OceanBase 存储设计要点（语法已由 03-oceanbase.md 官方文档核实，MySQL 模式）
**重要修正**：MySQL 模式下几何类型是标准类型名（POINT/LINESTRING/POLYGON/MULTI* 等 8 种），不是 SDO_GEOMETRY（SDO_GEOMETRY 是 Oracle 模式叫法）；建表必须 `列名 POINT NOT NULL SRID 4326`，且部署后要先导入 SRS 元数据。
核心表：
- dim_poi：poi_id, name, category_id, brand, address, city, location POINT NOT NULL SRID 4326, attrs JSON, status, ..., emb VECTOR(1024)；`SPATIAL INDEX idx_poi_loc(location)`（内部存 MBR，仅局部索引，列必须 NOT NULL+列级 SRID 否则索引不生效）；`VECTOR INDEX idx_poi_emb(emb) WITH(distance=cosine, type=hnsw, m=16, ef_construction=200, ef_search=64)`
- dim_category / dim_brand
- fact_store：门店/候选铺位 id, rent, area, location POINT NOT NULL SRID 4326, emb, 标签
- fact_traffic_grid：百米网格客流（grid_id, grid_poly POLYGON SRID 4326, time_bucket, cnt）
- fact_trajectory / fact_od：轨迹（猎鹰）
- scene_solution：会话状态快照（session_id, scene, version, state JSON, created_at）
- knowledge_doc：知识库切片 + VECTOR
已核实函数（写文档时分类列入，区分已查证/待实测）：
- 构造/输出：ST_GeomFromText(wkt, srid)、ST_GeomFromWKB、ST_AsText、ST_AsGeoJSON
- 关系：ST_Intersects、ST_Contains、ST_Within、ST_Distance(g1,g2[,unit])、ST_Distance_Sphere（球面米）、ST_IsValid、ST_Crosses、ST_Overlaps（4.3.1 增补）
- OB 私有：`_ST_Dwithin(g1,g2,distance)`（距离圈选关键，PG 兼容写法）、`_ST_Covers`、`_ST_MakeEnvelope`、`_ST_Buffer`、`_ST_ClipByBox2D`、`_ST_Transform`、ST_AsMVTGeom
- 属性/运算：ST_Area、ST_Length、ST_Centroid、ST_X/ST_Y、ST_Longitude/Latitude、ST_Buffer/ST_Buffer_Strategy、ST_Transform、ST_Difference、ST_Union（二元）、ST_SymDifference
- 向量：l2_distance / cosine_distance / inner_product 距离函数；ANN 必须 `ORDER BY cosine_distance(emb,:qvec) APPROXIMATE LIMIT k`（APPROXIMATE+ORDER BY+LIMIT 三要素，无 <=> 算子）；VECTOR 最多 16000 维，HNSW 4.3.3≤2000 维仅 l2/inner_product，4.4.0 起 cosine、≤4096 维
- 待实测：ST_Union_Agg（大概率不存在，用 GROUP BY + ST_Contains 连接替代）、ST_Intersection 独立函数、MBR* 函数族、空间连接专门优化算子、SPATIAL 与 HNSW 索引是否同一条 SQL 双走
典型混合查询（官方 V4.4 模型：向量 ANN 召回后 WHERE 标量/空间过滤）：
```sql
SELECT poi_id, name, ST_Distance_Sphere(location, ST_GeomFromText(:pt,4326)) AS dist_m,
       cosine_distance(emb, :qvec) AS sem_dist
FROM dim_poi
WHERE category_id = :cat AND status = 1
  AND _ST_Dwithin(location, ST_GeomFromText(:pt,4326), :radius_m)
ORDER BY cosine_distance(emb, :qvec) APPROXIMATE
LIMIT 20;
```
- 两种执行策略均写入文档并要求 EXPLAIN 实测：
  (A) 向量驱动：ANN 召回 + 空间/标量后过滤（官方文档语义，适合空间圈大、语义选择性强）；
  (B) 空间/标量驱动：先 SPATIAL INDEX 过滤物化候选（CTE/派生表），再对候选做向量排序（适合空间圈小、候选少）；
  (C) 兜底：空间/标量过滤 → 应用层取 emb 批量重排（仅小数据量/演示）。
- 空间下推：候选生成用 SQL；空间连接（POI JOIN 网格/竞品，ST_Contains/ST_Distance 谓词）、分组聚合（GROUP BY 网格 COUNT/AVG）在 OB 内完成；可达域多边形由高德 API 返回后作为 ST_Contains 参数传入。
- 官方文旅 demo 可直接参考其数据管道：github.com/oceanbase-labs/TravelAssist（Kaggle 景点 → 高德补经纬度 → BGE-m3 → OB 一表标量+POINT+VECTOR → Streamlit+Qwen）。
- 明确边界表（OB vs 高德 API）写入文档。

## 7. 两条端到端时序（要点）
### 7.1 智能选址
用户自然语言+圈选 → L1 手势约束 → L2 意图/约束（空间=圈、标量=业态/租金、语义="年轻人多、客流大"）→ Skill: site_selection.build_plan
→ ① 多模检索拉候选（OB 一条 SQL）
→ ② 空间计算：候选-竞品/客流格子的空间连接与聚合（OB 下推）；可达性（高德 API：等时圈/路径）
→ ③ 特征加工+评分（打分口径在 Skill，数据在 OB）
→ ④ Decision 综合 → Map Spec（候选点+热力+等时圈+排名）→ L1 渲染
→ 用户"把范围往东挪 1 公里/租金上限降到 8 万" → 依赖图标记 → 仅重算 ②③④ 受影响节点 → 增量渲染。

### 7.2 多模混合检索
"找附近适合自习、安静、能停车的咖啡馆" → 意图=POI 检索 → 空间约束=定位点 3km；标量=品类咖啡、营业中、有停车场；语义="适合自习、安静"→embedding
→ Query Builder 编译一条 SQL（空间范围 + 标量 + 向量 ANN TopK）
→ OB 走空间索引/向量索引 → 返回候选 + 分数解释
→ 逆地理/详情（必要时高德）→ Map Spec 标点 + 列表 → 反馈（展开/差评）回写。

## 8. 技术栈建议（待调研复核）
- 后端：Python 3.11+、FastAPI、Pydantic v2、SQLAlchemy（或直接 pymysql 驱动）、OceanBase 4.3.x Docker
- Agent：LangGraph（状态图+checkpointer+中断恢复）、MCP Python SDK（能力层对外暴露）
- 前端：Vue3/React + 高德 JS API 2.0 + Loca（组件式封装 Map Spec 渲染器）
- 模型：Embedding bge-m3（或 API）；LLM 豆包/DeepSeek（OpenAI 兼容），LLM 网关抽象
- 数据：pandas + 离线导入脚本；缓存 Redis（可选）
- 工程：uv/poetry、pytest、Docker Compose、ruff/mypy

## 9. 场景矩阵（挂载方式：Skill 包 + 数据表 + 图层模板）
| 场景 | 原子能力 | OB 侧数据/计算 | 高德侧 API | 图层 |
|---|---|---|---|---|
| POI 智能检索 | 地理编码/多模检索/详情 | poi 表 + 混合 SQL | 搜索/逆地理（兜底） | 点标记 |
| 智能选址 | 检索/空间连接/聚合/评分 | 门店+客流+竞品，ST 聚合 | 等时圈/路径 | 点+热力+多边形 |
| 客流洞察 | 时空聚合/热力 | fact_traffic 聚合 | 行政边界 | 热力/网格 |
| 人群分析 | 画像+区域统计 | 人群特征表+空间连接 | 边界 | 面填充 |
| 可达性分析 | 等时圈/路径 | 结果缓存与空间包含 | 路径规划/距离矩阵 | 等时圈/路线 |
| 轨迹分析 | 围栏/纠偏/OD | 轨迹表+ST 关系 | 猎鹰 | 线+围栏 |
| 门店运营 | 商圈/对比/预警 | 指标聚合 | 天气/路况 | 仪表盘 |
| 文旅行程 | POI 检索+路径排序 | POI+向量语义 | 多模式路径规划 | 路线+点 |

## 10. 路线图
- 阶段 0（赛题发布前·地基）：OB 环境 + 扩展能力实测清单（函数/向量/混合 SQL EXPLAIN 验证）；DDL 与数据模型；数据采集与导入（一个城市的样例数据）；Skill 基类 + Registry + MCP Server 骨架；Spatial State 与 LangGraph 7 步骨架；Map Spec 渲染器；**打通"多模混合检索"一个样例场景（最小闭环）**。
- 阶段 1（发布后适配）：按任务书参数化场景 Skill、补数据、对齐评分口径。
- 阶段 2（打磨）：局部重算、反馈记忆、压测（混合查询索引选择）、演示与文档。

## 11. 风险与备选
- 混合 SQL 中空间索引与向量索引不能同时被优化器选择 → 备选：两阶段查询（空间/标量过滤物化候选集 → 向量 ANN 重排），或向量预筛+空间过滤，文档中给 EXPLAIN 实测计划。
- OB 空间函数覆盖不足（无 ST_Buffer/等时圈）→ 高德 API 兜底 + 应用层极简几何（只做结果拼装不做计算）。
- 向量索引版本/社区版限制 → 纯 IVF/暴力扫描降级（数据量小可接受）。
- 赛题数据形态未知 → 数据模型用 JSON attrs + 可加 migration 吸收变化。
- 高德 key 配额 → 缓存、批量、离线采集。
