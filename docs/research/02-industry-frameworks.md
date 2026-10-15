# 工业界案例与 Agent 框架调研

> 服务项目：GeoMind（2026 OceanBase 数据库大赛 · 高德赛道）
> 调研时间：2026-10-15。所有链接均为本次联网实际打开/检索核实；事实标注为「已查证（附链接）」，推断或未能逐页核实的标注为「推测/未核实」。
> 核心设计约束回顾：空间计算下推 OceanBase 内核（SDO_GEOMETRY + R-Tree/SPATIAL_INDEX）、空间+标量+向量三模混合检索、"持续更新的结构化空间状态"、用户每次修改触发局部重算、7 步闭环（理解意图→建立约束→生成候选→空间计算→数据决策→地图表达→用户反馈）、插件化可扩展。

---

## A 工业界案例

### A1. 高德地图 MCP Server（官方 + 社区两个版本）

**提供方 / 链接**
- 官方文档：https://lbs.amap.com/api/mcp-server/gettingstarted ；更新日志：https://lbs.amap.com/api/mcp-server/changelog 【已查证】
- 社区 Python 包 `amap-mcp-server`：https://pypi.org/project/amap-mcp-server/ 【已查证】

**机制摘要**
- 官方版（2026-03-17 更新）：高德官方把 LBS Web 服务封装为 MCP Server，"覆盖 12 大核心服务接口"——地理编码、逆地理编码、IP 定位、天气查询、骑行/步行/驾车/公交路径规划、距离测量、关键词搜索、周边搜索、详情搜索。传输支持 **Streamable HTTP（推荐）** 与 **Node.js I/O（stdio）**，客户端为 Cursor / Claude / Cline 等任意 MCP Host。【已查证】
- 社区版 `amap-mcp-server` v0.1.11（2026-01-10 发布，维护者 0001coder，Python≥3.10，MIT），工具命名为 `maps_geo / maps_regeocode / maps_ip_location / maps_weather / maps_bicycling_* / maps_direction_walking|driving|transit_* / maps_distance / maps_text_search / maps_around_search / maps_search_detail`，每个工具都提供"经纬度版"和"地址版"两种入参（地址版内部先 geocode 再算路）。**同时支持 stdio / SSE / streamable-http 三种传输**，通过环境变量 `AMAP_MAPS_API_KEY` 鉴权。【已查证】
- 注意区分：官方版是高德出品的托管/服务化 MCP；PyPI 的 `amap-mcp-server` 是个人开发者封装，工具粒度更细但非官方背书。

**对 GeoMind 的可借鉴点**
- "地址版/坐标版双入参"是很好的工具设计：LLM 自然语言多给地址，让工具内部消化 geocoding，减少 LLM 多跳。
- 高德官方 MCP 只覆盖"在线 LBS 基础服务"（搜 POI、算路、地理编码），**不含空间计算下推数据库、不含客流/选址/选址叠加分析**——这正是 GeoMind 用 OceanBase 补位的差异化空间。
- 不足：它是"无状态工具集"，没有结构化空间状态、没有多步闭环、没有人机确认环节；用户每说一句话都要 LLM 重新拼参数。

### A2. 高德千舆 · 空间智能开放平台 / 高德云睿时空智能体平台

**提供方 / 链接**
- 千舆空间智能开放平台首页：https://lbsai.amap.com/ 【已查证首页存在】
- 高德云睿（时空智能体平台）：https://yunai.amap.com/ 【已查证】
- 空间智能 Agent 解决方案：https://lbs.amap.com/solution/spatialIntelligence 【已查证】
- 2026 云栖发布报道：http://economy.gmw.cn/2026-09/24/content_39019474.htm 【已查证】

**机制摘要**
- 2026 云栖大会正式发布"高德千舆空间智能开放平台"，把 8000 万+ POI、500 万公里道路、7500 km² 三维模型等时空时序数据，按"点·线·面·动态"框架组织为"AI 可理解、可计算的空间数据体系"，模块化标准服务、开箱即用。【已查证】
- 高德云睿把时空智能能力封装为 **MCP + 业务能力**，明确"连接常驻人口、客流、POI 与行政区查询能力，为 AI Agent 提供标准化时空智能调用入口"，上层覆盖区域洞察、选址分析、商圈评估、报告生成与文档处理。【已查证】
- 空间智能 Agent 解决方案强调支持 **SDK / API / A2A 协议** 多种调用方式，输入 Query 及位置信息即可，面向"人-时-地-事"的空间知识与环境感知。【已查证】
- 任务书所述"客流洞察 / 人群分析 / 智能选址 / 门店运营四大 Skill"与"AI 云工作台 / Agent / Skill·MCP Server / 标准 API 四种交付形态"：与上述官方页面方向一致，但具体四分法命名未能在可静态抓取的页面逐字核对【推测/未核实，建议以高德商务/开发者后台最新文档为准】。

**对 GeoMind 的可借鉴点**
- 这是与 GeoMind 最同构的业界范式：**把空间能力拆成可被 Agent 编排的 Skill/MCP 单元，而不是一个巨型聊天框**。GeoMind 的插件化架构应对标此分层。
- "MCP 数据能力层（常驻/客流/POI/行政区查询）+ 商业业务能力层（选址/商圈/报告）"是清晰的两层：GeoMind 可对应"MCP 原子工具层（OceanBase 空间 SQL）+ Skill 编排层（选址/可达性/客流分析）"。
- 不足/启示：高德云睿是商业闭平台，数据与算力在高德侧；GeoMind 的卖点应是**用 OceanBase 把空间计算下推到自有数据库内核**（可复用、可赛题演示、不依赖高德商业 API 配额）。

### A3. Mapbox MCP Server（及 Agent Toolkit）

**提供方 / 链接**
- 官方文档：https://docs.mapbox.com/api/guides/mcp-server/ （当前 v0.12.0）【已查证】
- GitHub：https://github.com/mapbox/mcp-server ；npm 包 `@mapbox/mcp-server`；托管端点 `https://mcp.mapbox.com/mcp`【已查证】
- DevKit MCP Server（面向开发者编码助手）：https://docs.mapbox.com/location-ai/mcp-servers/devkit-mcp-server/ 【已查证】
- BUILD 2026 Agent Toolkit 公告：https://www.mapbox.com/blog/top-announcements-from-build-with-mapbox-2026 【已查证】

**机制摘要（工具清单，极值得逐类借鉴）**
- **Offline Geospatial Tools（本地确定性空间计算，Turf.js，不发 API 请求）**：`distance_tool`(Haversine)、`bearing_tool`、`midpoint_tool`、`centroid_tool`、`area_tool`、`bounding_box_tool`、`buffer_tool`、`simplify_tool`(Douglas-Peucker)、`destination_tool`、`length_tool`、`convex_tool`、`union_tool`。【已查证】
- **Search & Navigation**：`search_and_geocode_tool`、`category_search_tool`、`reverse_geocode_tool`、`ground_location_tool`("给定坐标先问附近有什么"，建议作为坐标类问题的首选工具)、`place_details_tool`。
- **Advanced Routing**：`directions_tool`、`matrix_tool`（多点出行时间矩阵）、`isochrone_tool`（等时圈/可达性）、`optimization_tool`（TSP）、`map_matching_tool`。
- **Visualization**：`render_map_tool`（在支持 MCP Apps 的客户端内直接渲染交互式 Mapbox GL JS 面板，可接受别的工具返回的 `payload_refs`，也接受手写 GeoJSON）、`static_map_image_tool`（base64 PNG，兜底）。
- **Resources**：`mapbox://categories` 等静态参考数据；**Elicitation**：当结果/路线有歧义时反向向用户追问消歧；支持 `--enable-tools/--disable-tools` 裁剪工具集。【已查证】
- BUILD 2026 还发布了 Maps SDK / Navigation SDK 的 **Agent Toolkit**，让 Agent 通过 35+ 地图/导航控件读写地图上下文。【已查证】

**对 GeoMind 的可借鉴点（最高价值案例）**
- **"离线确定性空间计算工具"这一层设计就是 GeoMind 要在 OceanBase 里做的事**：Mapbox 用 Turf.js 在客户端本地算 buffer/area/union/centroid；GeoMind 应把这些**同构空间函数下推为 OceanBase 的 `ST_Buffer / ST_Area / ST_Union / ST_Centroid / ST_Distance_Sphere`（R-Tree 索引加速）**，并包装成 MCP 工具。这既是赛题考点，也是差异化叙事。
- `ground_location_tool` 作为"坐标类问题首选入口"的路由约定、`render_map_tool` 直接回 GeoJSON 交互地图面板、Elicitation 消歧——三者可直接搬到 GeoMind：地图表达节点回 GeoJSON/Loca，歧义节点用 interrupt 问用户。
- 不足：Mapbox 的空间计算在应用/Turf 侧而非数据库内核，不做"空间+标量+向量"三模混合检索，也没有持久化的结构化空间状态闭环。

### A4. Google Maps / Google Maps Platform 的 AI Agent 能力

**提供方 / 链接**
- Maps Grounding Lite MCP（面向 Agent 的真实地理背景）：https://developers.google.com/maps/architecture/grounding-with-maps-mcp 【已查证】
- 工具参考（`mapstools.googleapis.com`）：https://developers.google.com/maps/ai/grounding-lite/reference/mcp/search_places 【已查证】
- Google Maps Platform Code Assist Toolkit（面向开发者的 MCP）：https://developers.google.com/maps/ai/mcp 【已查证】
- Google ADK 接入 MCP：https://google.github.io/adk-docs/tools-custom/mcp-tools/ 【已查证】

**机制摘要**
- **Maps Grounding Lite MCP** 给 LLM 提供三类知识库检索工具：`search_places`（返回 AI 生成的地点摘要 + 每个地点的 place_id、经纬度、Google Maps 链接，place_id 可再交给其他 Maps Platform API 上图）、天气查询等。定位是"给 Agent 做真实世界地理 grounding"，可在任意支持 MCP 的工具里开启。【已查证】
- **Code Assist Toolkit** 是另一类 MCP：面向 AI 编码助手，只读访问 Maps Platform 文档、服务条款、Trust Center、官方 GitHub 仓库——是"开发辅助 MCP"，不是运行时业务 Agent。【已查证】
- Google ADK（Agent Development Kit）通过 `McpToolset` + `StdioConnectionParams` 把任意 MCP Server 挂成 Agent 工具。【已查证】

**对 GeoMind 的可借鉴点**
- `search_places` 返回"摘要 + 结构化 id/坐标 + 可上图链接"的范式，提示 GeoMind 的 POI 检索工具应返回**结构化候选（id、坐标、标量属性）而非自然语言段落**，供下游空间计算节点消费。
- 不足：Google 侧 MCP 也是"在线数据 grounding"，不做数据库内三模混合检索与空间计算下推；与 GeoMind 赛道不直接竞争。

### A5. OceanBase 官方空间/向量/多模 RAG 开源 demo

**提供方 / 链接**
- 官方教程《基于 OceanBase 多模融合构建文旅小助手》（V4.5.0）：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000004475454 【已查证】
- 代码仓库：https://github.com/oceanbase-devhub/ob-multi-model-search-demo 【已在官方文档中给出并据此克隆】
- OceanBase × LlamaIndex 集成：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000004481735 【已查证】
- 向量能力（4.3.3 GA）社区博客：https://open.oceanbase.com/blog/15581148192 ；以图搜图 https://github.com/oceanbase-devhub/image-search 【已查证】
- 空间类型/函数文档：SDO_GEOMETRY 查询函数 https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000005288404 ；空间关系函数 https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000644024 ；空间运算函数 https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000644025 ；创建空间索引 `INDEXTYPE IS MDSYS.SPATIAL_INDEX` https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000005282987 【均已查证】

**机制摘要（这是 GeoMind 的基线参考实现）**
- 官方明确定义"多模融合 = 向量数据 + 空间数据 + 文档数据 + 标量数据融合查询"，依赖**向量索引 + 空间索引 + 全文索引**三类索引做混合搜索。【已查证】
- 文旅小助手技术栈：Python 3.10+ / Poetry / **pyobvector 0.2.16** / **Streamlit** 前端；LLM 用阿里云百炼（`DASHSCOPE_API_KEY`），地理服务用**高德 LBS（`AMAP_API_KEY`）**；数据用 Kaggle《中国城市景点》CSV；流程是数据预处理导入 OB → Streamlit 聊天界面。【已查证】
- 空间能力：Oracle 模式 `SDO_GEOMETRY` + `MDSYS.SPATIAL_INDEX`（R-Tree 类空间索引）；空间关系 `ST_Intersects / ST_Contains / ST_Distance / ST_Distance_Sphere`；空间运算 `ST_Buffer / ST_Buffer_Strategy / ST_Transform / ST_Union / ST_Centroid / ST_AsGeoJSON`；MySQL 模式 GIS 在 4.3.1 增强。向量类型自 4.3.3 引入。【已查证】

**对 GeoMind 的可借鉴点**
- **直接站在它肩上**：pyobvector 数据导入、三模索引、高德 key + 百炼模型的组合已被官方跑通，GeoMind 不必重造数据管道。
- 它的不足正是 GeoMind 的增量：官方 demo 是"Streamlit 单轮 RAG 问答"，**没有 LangGraph 状态图、没有持久化结构化空间状态、没有 7 步闭环、没有 HITL、没有 MCP 插件化**。GeoMind = 官方多模检索底座 + Agent 编排层 + 空间状态闭环。

### A6. 其他值得关注的地图/空间 Agent 案例

- **高德云睿（yunai.amap.com）**：已并入 A2，代表"常驻/客流/POI/行政区"空间数据能力的 MCP 化。【已查证】
- **Mapbox Agent Toolkit（Maps/Navigation SDK，35+ 控件）**：代表"Agent 直接读写地图 UI 上下文"的方向，GeoMind 前端（高德 JS API/Loca）可参考其"地图控件即工具"思路。【已查证】
- **RAGFlow × OceanBase seekdb**：说明 OceanBase 向量能力已被主流 RAG 框架作为后端适配，佐证"OB 作多模检索底座"的可行性。【已查证，CSDN 转载官方博客 https://oceanbase.csdn.net/698c1eaf54b52172bc5b1859.html 】
- 未发现公开的、把"空间计算下推数据库内核 + MCP Skill + 结构化空间状态闭环"三者打通的开源完整案例【检索结论：未核实到等价物，正是 GeoMind 的创新空位】。

---

## B Agent 工程框架

### B1. LangGraph（推荐主框架）

**链接**：Checkpointers https://docs.langchain.com/oss/python/langgraph/checkpointers ；Persistence（checkpointer + store）https://docs.langchain.com/oss/javascript/langgraph/persistence ；Subgraphs https://docs.langchain.com/oss/python/langgraph/use-subgraphs ；Interrupts/HITL https://docs.langchain.com/oss/javascript/langgraph/interrupts 【均已查证，2026-10 仍在更新】

**机制摘要**
- **StateGraph**：把工作流建模为"共享状态对象 + 节点(函数) + 边(含条件边)"，状态在节点间显式传递。与 LangChain 的关系：LangChain 提供模型/工具/检索等原子件，LangGraph 是其上层**低级有状态编排层**（`create_react_agent` 是其预置的 ReAct 封装）。【已查证】
- **Checkpointer（检查点）**：每个 super-step 把图状态快照落盘，按 `thread_id` 组织成线程；由此免费获得**会话记忆、故障恢复、time-travel、human-in-the-loop**。【已查证】
- **Store（长期记忆）**：与 checkpointer 互补——checkpointer 管"线程内短期/工作状态"，store 管"跨线程长期数据"（用户偏好、共享事实）。【已查证】
- **HITL**：用 `interrupt()` 在任意节点动态暂停，人工检视/修改状态后用 `Command(resume=...)` 恢复；支持在歧义处向用户追问。【已查证】
- **Subgraph**：子图分"per-invocation（每次调用全新，继承父 checkpointer）"与"per-thread（同线程累积状态）"两种模式，天然支持把"选址/可达性/客流分析"做成可复用子图。【已查证】
- 流式：支持 token/节点/自定义多粒度流式输出。【已查证】

**对 GeoMind 的支持方式（关键映射）**
- "持续更新的结构化空间状态" = StateGraph 的 **TypedDict/Pydantic 共享状态**（含候选集 GeoJSON、约束、当前 bbox、过滤条件）；checkpointer 按会话 thread 持久化它。
- "用户每次修改触发局部重算" = 状态变更后经**条件边只重算受影响节点**（约束变了→重跑候选生成与空间计算，不必重跑意图理解）。
- "7 步闭环" = 一条主 StateGraph：理解意图 → 建立约束 → 生成候选 → 空间计算(SQL下推) → 数据决策 → 地图表达 →（interrupt 等待用户反馈）→ 条件边回到对应步骤。
- HITL interrupt 正好落在"生成候选后请用户确认/改约束"。

### B2. MCP（Model Context Protocol）— 空间能力插件化的事实标准

**链接**：Architecture https://modelcontextprotocol.io/docs/learn/architecture ；Transports https://modelcontextprotocol.io/specification/2025-11-25/basic/transports ；2026-07-28 规范 RC 公告 https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/ 【均已查证】

**机制摘要**
- **角色**：Host（如 Claude/自研后端）→ 每个 Server 起一个 Client 连接；Server 向 Client 暴露能力。基于 **JSON-RPC 2.0**。【已查证】
- **三种 Server 原语**：**Tools**（可执行动作，如 API/SQL 调用，`tools/list` 动态发现 + `tools/call`）、**Resources**（上下文数据，如 schema、参考数据，URI 寻址）、**Prompts**（可复用交互模板）。另有 Client 侧原语 Sampling/Elicitation/Logging。【已查证】
- **传输**：**stdio**（本地子进程，零网络开销）与 **Streamable HTTP**（远程，HTTP POST + 可选 SSE，支持 bearer/OAuth）；旧的独立 SSE 传输已并入 Streamable HTTP。【已查证】
- **新动向**：2026-07-28 RC 引入无状态核心、Extensions 框架、**Tasks（长时/可延迟取回的耐久执行）**、**MCP Apps（富 UI 面板，Mapbox `render_map_tool` 即用此）**、授权加固。【已查证】

**为什么适合做空间能力插件化封装**
1. **动态发现 + JSON Schema 入参**：LLM/编排层启动时 `tools/list` 自动拿到全部空间工具及其参数，新增一个空间算子（如"商圈 3km 内奶茶店密度"）只需注册一个新 tool，不动主图——天然满足"赛题未发、需插件化可扩展"。
2. **富内容返回**：tool 返回 content 数组支持 text/image/结构化，GeoJSON 可直接交给前端 Loca；MCP Apps 还能回交互地图面板。
3. **远程 Streamable HTTP**：OceanBase 空间 SQL 工具可作为独立 MCP Server 部署，与 LangGraph 后端解耦，便于评测与替换。
4. **Elicitation**：歧义地点/多候选可反向问用户，对应 7 步的"用户反馈"。
> 说明：MCP 官方规范本身没有强制的 "Skill" 一等概念；业界/Skill市场里的 "Skill" 通常是"一组相关 MCP Tool + Prompt + 说明文档"的打包约定。高德/Mapbox 的"Skill/MCP"即此用法【推测/约定，非规范原文】。

### B3. AutoGen（微软）与 CrewAI —— 多智能体编排取舍

**链接（框架对比，2025-10 仍活跃）**：https://learnwithparam.com/blog/choosing-ai-agent-framework-langgraph-llamaindex-crewai-autogen ；https://www.1raft.com/blog/ai-orchestration-platform-guide 【已查证】

**机制摘要**
- **CrewAI**：以"角色(Role) + 任务(Task) + Crew"建模，偏"团队流水线"，角色间顺序/层级交接。
- **AutoGen（Microsoft）**：以"可对话的 Agent(ConversableAgent) + 显式 handoff"建模，偏"多 Agent 自由对话/辩论"。
- 业界共识（多篇 2025/2026 综述一致）：多智能体只在三种情况才划算——**单上下文窗口装不下需分发**、**需要并行执行**、**需要多套专精人格互相校验**；若是单 LLM + 几个工具、线性或有分支的流程，单智能体 + 状态图更简单、更可控、token 更省。【已查证】

**对 GeoMind 的取舍（建议）**
- GeoMind 的核心是**确定性空间计算/SQL**，步骤强依赖共享的结构化空间状态，不是"多专家辩论"。**不建议**用 AutoGen/CrewAI 做主编排。
- 可借鉴点：若后期要加"一个 Agent 出选址方案 + 一个 Agent 做反方挑错/校验"，可用 LangGraph 的双节点或 handoff 子图实现，不必引入整套多智能体框架。

### B4. Plan-and-Execute vs ReAct —— 重确定性计算场景的规划模式

**链接**：LangChain 官方博客 Plan-and-Execute https://www.blog.langchain.com/planning-agents/ ；2026 对比 https://www.agentnative.dev/compare/react-vs-plan-and-execute-for-agent-loops 【已查证】

**机制摘要**
- **ReAct**：Thinking→Acting→Observing 交错循环，每步一次 LLM 调用、就地决定下一步；优点是对随机/不确定环境反应灵活，缺点是长任务下上下文膨胀、易"推理漂移"、每步都要重看全部历史。【已查证】
- **Plan-and-Execute**：Planner 先一次性产出多步计划，Executor 逐步执行（可用小模型/确定性 runner），每步后 Replanner 决定结束/继续/改计划。官方与多方实测：**编译后的计划比纯 ReAct 省 3–5× token**、计划本身是可审计的意图记录、步骤可并行。【已查证】
- 切换建议：任务可预测分解、≥6 步、步骤间不强依赖上一步观测、追求 token/可审计性 → Plan-and-Execute；环境高度随机、每步结果都要改变下一步 → ReAct。【已查证】

**对 GeoMind 的建议（混合模式）**
- 空间计算/SQL 是**确定性、可预测、可审计**的，不适合纯 ReAct 每步让 LLM 重新决策。
- **推荐混合**：外层用 StateGraph 把 7 步固化为有向流程（≈ Plan-and-Execute 的"计划"由图结构承担，可审计、不漂移）；只在**"理解用户意图 / 消歧 / 决定是否重规划"这两个节点**用 ReAct 式 LLM 调用；空间计算节点是**纯确定性 SQL 下推，不调 LLM**。这样既省 token、又可控、又保留对自然语言模糊意图的适应性。

### B5. LlamaIndex 与 Dify 在本场景的角色（补充）

**链接**：OceanBase × LlamaIndex 集成 https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000004481735 【已查证】

- **LlamaIndex**：定位 data/RAG-first（Query Engine、Retriever、索引抽象）。OceanBase 已提供 LlamaIndex 向量集成。**角色建议**：把它当作"检索/嵌入/三模混合召回"的工具库使用（尤其召回阶段），但**不用它做长会话、结构化状态、HITL 的工作流编排**——那是 LangGraph 的职责。
- **Dify**：低代码 Agent 应用平台，适合快速搭 demo/给非工程同学可视化编排。**角色建议**：可作为前期原型或答辩演示的备选壳，但对"结构化空间状态 + 局部重算 + SQL 下推精细控制"的掌控力弱于 LangGraph，正式参赛主代码建议用 Python+FastAPI+LangGraph 自托管。

---

## 技术选型建议（GeoMind 视角）

> 标注：【事实】= 上文已查证；【建议】= 基于设计约束的推断。

1. **编排框架：LangGraph（首选），不用 AutoGen/CrewAI 做主框架。**【建议，依据 B1/B3】
   - 理由：赛题核心是"持久化结构化空间状态 + 用户改动触发局部重算 + 7 步闭环 + HITL 反馈"，这正是 LangGraph 的 StateGraph + checkpointer + interrupt + 条件边原生覆盖的能力；多智能体辩论范式对确定性空间 SQL 是过度设计。
2. **规划模式：混合 = 固定状态图（≈Plan-and-Execute 的可审计骨架）+ 仅意图/消歧节点用 ReAct + 空间计算节点纯确定性 SQL。**【建议，依据 B4】
   - 理由：空间计算/SQL 可预测、可审计、追求省 token 与不漂移；把"计划"固化进图结构，避免纯 ReAct 的推理漂移。
3. **MCP 用法：把 OceanBase 空间能力封装为独立 MCP Server（Streamable HTTP）。**【建议，依据 B2/A3/A5】
   - 工具分层直接对标 Mapbox：① 原子空间算子（`ST_Buffer/ST_Contains/ST_Distance_Sphere/ST_Union/ST_Area/ST_Centroid`，走 R-Tree 下推）；② POI/三模混合检索（向量+标量+空间，pyobvector）；③ 业务 Skill 子图（智能选址、可达性等时圈、客流热力）。新增赛题能力 = 注册新 MCP tool，不动主图。
   - 高德在线 LBS（geocode/算路/天气）按 A1 方式作为另一组 MCP tool 接入，与自有 OB 计算互补。
4. **空间计算下推数据库（核心差异化）**：以 Mapbox 的 Turf.js 本地算子为镜像，把同样的 buffer/area/union/centroid/distance 落到 OceanBase `SDO_GEOMETRY` + `MDSYS.SPATIAL_INDEX`（R-Tree）+ `ST_*` 函数【事实，A5】，在数据库内完成空间连接/聚合，避免把几何拉到应用层算。
5. **结构化空间状态**：Pydantic Typed State（候选 GeoJSON 集合、约束、bbox、过滤条件、已确认项）+ LangGraph checkpointer 按 thread 持久化；条件边实现"改约束→只重算下游节点"。
6. **地图表达与反馈**：地图节点回 GeoJSON（前端高德 JS API/Loca 渲染），对标 Mapbox `render_map_tool` 的富面板思路；歧义/多候选用 `interrupt` 或 MCP Elicitation 向用户确认，闭环回主图。
7. **基座复用**：数据管道直接参考官方 `ob-multi-model-search-demo`（pyobvector + 高德 key + 百炼/DashScope），不重复造轮子。

---

## 参考链接清单（均本次实际检索/打开核实）

**A. 工业界**
- 高德官方 MCP Server 快速接入：https://lbs.amap.com/api/mcp-server/gettingstarted
- 高德官方 MCP Server 更新日志：https://lbs.amap.com/api/mcp-server/changelog
- 社区 amap-mcp-server (PyPI)：https://pypi.org/project/amap-mcp-server/
- 高德千舆空间智能开放平台：https://lbsai.amap.com/
- 高德云睿时空智能体平台：https://yunai.amap.com/
- 高德空间智能 Agent 解决方案：https://lbs.amap.com/solution/spatialIntelligence
- 高德千舆云栖发布报道（光明网）：http://economy.gmw.cn/2026-09/24/content_39019474.htm
- Mapbox MCP Server 文档 v0.12.0：https://docs.mapbox.com/api/guides/mcp-server/
- Mapbox MCP Server GitHub：https://github.com/mapbox/mcp-server
- Mapbox DevKit MCP Server：https://docs.mapbox.com/location-ai/mcp-servers/devkit-mcp-server/
- Mapbox BUILD 2026 Agent Toolkit：https://www.mapbox.com/blog/top-announcements-from-build-with-mapbox-2026
- Google Maps Grounding Lite MCP：https://developers.google.com/maps/architecture/grounding-with-maps-mcp
- Google Maps Grounding Lite search_places 参考：https://developers.google.com/maps/ai/grounding-lite/reference/mcp/search_places
- Google Maps Platform Code Assist MCP：https://developers.google.com/maps/ai/mcp
- Google ADK MCP 接入：https://google.github.io/adk-docs/tools-custom/mcp-tools/
- OceanBase 多模文旅小助手教程：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000004475454
- OceanBase × LlamaIndex 集成：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000004481735
- OceanBase 4.3.3 向量能力博客：https://open.oceanbase.com/blog/15581148192
- OceanBase SDO_GEOMETRY 查询函数：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000005288404
- OceanBase 空间关系函数：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000644024
- OceanBase 空间运算函数：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000644025
- OceanBase 创建空间索引 MDSYS.SPATIAL_INDEX：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000005282987
- RAGFlow × OceanBase seekdb：https://oceanbase.csdn.net/698c1eaf54b52172bc5b1859.html

**B. 框架/协议**
- LangGraph Checkpointers：https://docs.langchain.com/oss/python/langgraph/checkpointers
- LangGraph Persistence（checkpointer + store）：https://docs.langchain.com/oss/javascript/langgraph/persistence
- LangGraph Subgraphs：https://docs.langchain.com/oss/python/langgraph/use-subgraphs
- LangGraph Interrupts / HITL：https://docs.langchain.com/oss/javascript/langgraph/interrupts
- MCP Architecture：https://modelcontextprotocol.io/docs/learn/architecture
- MCP Transports（2025-11-25 规范）：https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- MCP 2026-07-28 规范 RC 公告：https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/
- Agent 框架对比（LangGraph/LlamaIndex/CrewAI/AutoGen）：https://learnwithparam.com/blog/choosing-ai-agent-framework-langgraph-llamaindex-crewai-autogen
- AI 编排平台选型：https://www.1raft.com/blog/ai-orchestration-platform-guide
- LangChain 官方 Plan-and-Execute 博客：https://www.blog.langchain.com/planning-agents/
- ReAct vs Plan-and-Execute (2026)：https://www.agentnative.dev/compare/react-vs-plan-and-execute-for-agent-loops
