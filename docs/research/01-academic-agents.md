# 学术空间智能体调研

> 调研目的：为 GeoMind（2026 OceanBase 数据库大赛·高德赛道，方向「AI+数据库应用开发」，核心为「空间数据处理 + 空间/标量/向量三模混合检索」，基于 OceanBase 与高德开放平台构建空间智能体）的架构设计提供学术参照。
> 调研方式：arXiv 摘要页 / ACM / 机构主页逐一点开核实。每条事实标注「已查证（附链接）」或「推测/未核实」。
> 调研日期：2026-10-15。

---

## 1. GeoGPT（自主 GIS 任务求解的 GPT 框架）

- **名称 / 机构 / 时间**：GeoGPT: Understanding and Processing Geospatial Tasks through An Autonomous GPT。作者 Yifan Zhang, Cheng Wei, Shangyou Wu, Zhengting He, Wenhao Yu（国内高校团队，地理信息方向），2023-07-16 提交 arXiv。【已查证】
- **真实链接**：
  - arXiv 摘要页：https://arxiv.org/abs/2307.07930 【已查证，可打开】
  - PDF：https://arxiv.org/pdf/2307.07930
- **架构摘要**：
  - 以 LLM（实验用 gpt-3.5-turbo，temperature=0）为「大脑」，基于 **LangChain** 把 LLM 与 GIS 社区成熟工具串联，按非专业用户的自然语言指令，自主完成「地理数据采集 → 处理 → 分析 → 出图」。【已查证，来自 PDF 正文片段】
  - 数据流：用户自然语言 → LLM 语义理解 → **chain-of-thought（思维链）式 think-plan-execute** → 依次调用已定义 GIS 工具（如 Buffer 选址、Intersect/Erase 叠加分析、空间查询、爬取、制图）→ 输出结果。
  - 论文用设施选址（先 Buffer 定缓冲区，再 Intersect/Erase 选满足多约束区域）作为典型链路示例。【已查证，摘要页明确】
- **LLM 与 GIS 工具关系 / 规划模式**：
  - 属 **AutoGPT 式自主循环（类 ReAct：推理—行动—观察）**，把成熟 GIS 工具当作可被 LLM 自主编排的「动作集」；空间计算不在 LLM 内做，而是下沉到专业 GIS 工具执行。【已查证 + 推测：摘要称"think, plan, and execute defined GIS tools"】
- **记忆/状态 / 扩展机制**：
  - 通过 LangChain 的工具注册机制扩展；论文明确"可通过装备更多 GIS 工具扩展到更多任务"，提出「foundational + professional」范式。【已查证】
  - 论文未强调持久化结构化空间状态，中间结果以工具输出形式串接。【推测/未核实细节】
- **可借鉴点（对 GeoMind）**：
  1. 「基座大模型 + 专业空间工具」分工：LLM 只管意图理解与编排，空间计算交给确定性引擎。
  2. 选址链路天然是「Buffer→叠加→筛选」多步空间算子串联，可直接映射为 OceanBase OGC 函数（ST_Buffer / ST_Intersects / 空间连接）下推。
  3. temperature=0 + 固定工具集，保证空间计算可复现、可审计。
- **不足/局限**：
  1. 案例数量少（数据爬取、空间查询、选址、制图），未做系统 benchmark。
  2. 依赖外部 LLM API 与 LangChain，无向量检索、无三模混合查询概念。
- **对 GeoMind 的直接启发**：
  - 把 GeoGPT 的「链式空间算子」落到数据库内核：选址不是在 Python 里拼 GeoPandas，而是生成一条带 OGC 函数下推的 SQL，中间空间状态（候选区几何）以 `SDO_GEOMETRY` 形式存于 OceanBase，形成"持续更新的结构化空间状态"。

---

## 2. UrbanGPT（时空预测大模型，KDD 2024）

- **名称 / 机构 / 时间**：UrbanGPT: Spatio-Temporal Large Language Models。作者 Zhonghang Li, Lianghao Xia, Jiabin Tang, …, Chao Huang（香港大学 HKU 数据智能组）。**KDD 2024 Full Paper（ADS）**，arXiv 2024-02-25。【已查证】
- **真实链接**：
  - arXiv 摘要页：https://arxiv.org/abs/2403.00813 【已查证，Comments 明确 "Accepted by KDD'2024 as Full Paper"】
  - 代码：https://github.com/HKUDS/UrbanGPT 【已查证，检索摘要给出】
  - ACM DOI：https://dl.acm.org/doi/abs/10.1145/3637528.3671578 【已查证，第三方综述给出】
- **架构摘要**：
  - 不是「工具调用 Agent」，而是把**时空依赖编码器（spatio-temporal dependency encoder）**与 LLM 的指令微调（instruction-tuning）范式结合，让冻结/微调的 LLM 理解跨时间与跨空间的相互依赖，在数据稀缺下做零样本时空预测（交通、人口流动、犯罪等）。【已查证，摘要页】
  - 数据流：时空观测序列 → 时空依赖编码器建模 → 与文本指令对齐 → LLM 输出预测。
- **LLM 与空间计算关系**：空间/时间相关性被**编码进表征层**再喂给 LLM，而非 LLM 直接调用空间 API；属于"时空基础模型"路线，与 GeoMind 的"LLM 编排 + 数据库空间计算"路线互补。
- **记忆/状态 / 扩展机制**：通过时空指令微调覆盖多类城市下游任务；非插件式工具扩展。
- **可借鉴点**：
  1. 「城市要素 ≠ 单点 POI，而是带时空依赖的动态实体」——GeoMind 的客流热力/可达性分析要保留时间维。
  2. 零样本/低泛化思路：在标注稀缺时，用结构化指令把"查询意图"翻译成模型/系统能执行的算子。
- **不足/局限**：
  1. 面向预测而非交互式检索/选址，无工具调用、无数据库下推。
  2. 需要训练/微调，工程门槛高，不适合竞赛型即席 Agent。
- **对 GeoMind 的直接启发**：把"时空依赖"显式建模进结构化空间状态（POI 的营业时间、客流时段字段），使后续多模混合检索能同时约束空间范围 + 时间窗 + 标量条件。

---

## 3. RouteGPT / DynamicRouteGPT（路线规划 LLM 工作）

- **核实结论**：**未检索到恰好名为「RouteGPT」的独立学术论文/开源项目**。与"路线规划 + LLM"最贴近、且可核实的工作是 **DynamicRouteGPT**（arXiv 2408.14185）；另有 PathGPT（个性化路径生成，arXiv 2504.05846）。下文以 DynamicRouteGPT 为代表。【未找到 RouteGPT 同名；DynamicRouteGPT 已查证】
- **DynamicRouteGPT 信息**：
  - 作者 Ziai Zhou, Bin Zhou, Hao Liu，2024-08-26。链接：https://arxiv.org/abs/2408.14185 【已查证】
  - 架构：**四模块**——①实时信息检索模块（Real-Time Information Retrieval）；②备选路径生成模块（Alternative Path Generation）；③GPT 决策模块（GPT Decision）；④动态路径调整模块（Dynamic Path Adjustment）。【已查证，PDF 正文片段】
  - 方法上先用静态 Dijkstra 算全局最优基线，再在路口用 LLM（Llama3-8B）+ 马尔可夫链/贝叶斯做局部实时决策，并构造**因果图做反事实推理**以平衡全局最优与局部动态、给出可解释路径选择。【已查证，摘要页】
- **规划模式**：Plan-and-Execute 变体——全局离线算路（Dijkstra）+ 局部 LLM 决策 + 动态调整闭环；LLM 不自己算最短路径，只在"候选路径里做偏好/事件级选择"。
- **可借鉴点**：
  1. **全局确定性算法 + 局部 LLM 决策**的分层：复杂空间计算（最优路径）交给成熟算法/引擎，LLM 只处理自然语言偏好与异常事件。
  2. 把"实时信息检索"作为独立模块——对应 GeoMind 里高德实时路况/POI 状态接入。
- **不足/局限**：
  1. 仅 v1 草稿（12 页），工程化与多端对照有限。
  2. 关注车载动态导航，不涉及 POI 三模检索与数据库下推。
- **对 GeoMind 的直接启发**：路径类需求不要让 LLM "凭空算路"，而是：高德路径规划 API 出候选 → OceanBase 做沿途 POI/可达性的空间过滤 → LLM 按用户偏好（顺路、避开、评分）在候选中排序，形成"空间计算在引擎、偏好决策在 LLM"的闭环。

---

## 4. MapGPT（地图/空间对话智能体，同名甄别）

> **同名甄别（重要）**：检索到至少三个"MapGPT"，分属不同领域，不可混用：
> - **(A) 学术版 MapGPT（空间数据处理方向，本报告采纳）**：arXiv 2310.11029。
> - **(B) MapGPT（视觉语言导航 VLN，机器人方向）**：arXiv 2401.07314，在线语言地图辅助机器人探索，与 GIS/地图对话无关，**排除**。
> - **(C) Mapbox MapGPT（商业产品）**：Mapbox 官方 location-intelligence 助手，非学术，仅作产业参照。

- **采纳对象 (A)**：Core Building Blocks: Next Gen Geo Spatial GPT Application（文中自称 MapGPT）。作者 Ashley Fernandez, Swaraj Dube，2023-10-17。链接：https://arxiv.org/abs/2310.11029 【已查证】
- **架构摘要**：主张把 LLM 与空间数据处理结合，核心"building blocks"包括：在空间+文本数据上构建 LLM、**面向空间信息的专用 tokenization 与向量表示（spatial vector representations）**、以及在 MapGPT 内执行空间计算并输出可视化。【已查证，摘要页】
- **LLM 与空间计算关系**：试图让空间信息本身具备可嵌入的向量表示，再与 LLM 结合；偏"空间表征 + 计算可视化"的概念框架，而非成熟工具调用 Agent。
- **可借鉴点**：
  1. **显式的"空间向量表示"**意识——与 GeoMind 的向量检索维度直接相关（POI 语义向量化）。
  2. 自然语言 → 空间计算 → 可视化输出的端到端闭环主张。
- **不足/局限**：
  1. 是 building-blocks/愿景型短文（仅 2 页 PDF 体量），无系统实现、无 benchmark。
  2. 空间向量如何生成、如何与关系/空间查询融合，均未落地。
- **对 GeoMind 的直接启发**：把"空间向量"作为一等公民——OceanBase 同时存 `SDO_GEOMETRY`（空间）+ 标量属性 + POI 语义向量，三模混合检索正是这一愿景的数据库级实现。

---

## 5. TripGenie / TP-RAG（旅游规划 Agent 与工具调用 benchmark）

> **核实结论**：**TripGenie 是 Trip.com（携程集团）的商用 App 内 AI 旅行助手**（2023-07 发布，基于 LLM 做行程生成与预订），**不是学术 benchmark**。【已查证，Trip.com newsroom】
> 学术上最贴近"旅游规划 Agent + 时空合理性 + 检索增强 + benchmark"的工作是 **TP-RAG / EvoRAG**（EMNLP 2025）。下文以 TP-RAG 为学术代表。

- **TripGenie（商业，仅作背景）**：链接 https://www.trip.com/newsroom/introducing-tripgenie-groundbreaking-ai-travel-assistant/ 【已查证】——文本/语音对话生成行程并直接预订。
- **TP-RAG（学术）**：TP-RAG: Benchmarking Retrieval-Augmented LLM Agents for Spatiotemporal-Aware Travel Planning。作者 Hang Ni, Fan Liu, …, Hui Xiong, Hao Liu，2025-04-11，**EMNLP 2025 Main**。链接：https://arxiv.org/abs/2504.08694 ；DOI 10.18653/v1/2025.emnlp-main.626。【已查证】
- **架构 / benchmark 摘要**：
  - 首个面向"检索增强 + 时空感知"旅游规划的 benchmark：2,348 条真实旅行查询、85,575 个细粒度标注 POI、18,784 条高质量旅行轨迹参考（来自在线游记）。【已查证】
  - 提出 **EvoRAG** 进化式框架，把检索到的多样轨迹与 LLM 自身推理协同，提升时空合规性、减少常识违背。【已查证】
  - 关键发现：引入参考轨迹显著提升路线效率与 POI 合理性；但检索并非总是有益——在参考冲突/噪声下存在约 12.59% 的失败差距，普适性与鲁棒性仍是挑战。【已查证，PDF 片段】
- **规划模式**：RAG 增强的 LLM 规划 + 轨迹参考；评估维度含路线效率、POI 吸引力、实时适应性（时空合理性）。
- **记忆/状态 / 扩展**：以"历史轨迹/POI 知识库"作为外部检索源，非长期记忆式 Agent。
- **可借鉴点**：
  1. **POI + 轨迹作为可检索知识源**——GeoMind 可把高质量选址/路线案例做成向量库，做检索增强生成候选。
  2. "检索不一定有益、需去冲突"的教训：多模检索结果要由 LLM/规则做一致性校验，而非无脑拼接。
- **不足/局限**：
  1. 是 benchmark + 方法对比，不提供可复用的多智能体运行时。
  2. 检索源来自游记文本，非结构化数据库空间查询。
- **对 GeoMind 的直接启发**：三模混合检索的结果要经过"时空合理性校验"这一步（例如选出的 POI 是否落在同一可达范围内、时间是否冲突），对应高德 7 步闭环里的"建立约束→生成候选→空间计算→数据决策"。

---

## 6. AutoGIS / LLM-Geo（自主 GIS 概念框架）

- **名称 / 机构 / 时间**：Autonomous GIS: the next-generation AI-powered GIS。作者 **Zhenlong Li, Huan Ning（Penn State，地理信息与大数据研究实验室 GIBD）**，2023-05-10。链接：https://arxiv.org/abs/2305.06453 【已查证】
- **原型系统**：**LLM-Geo**（基于 GPT-4 API + Python 环境），是早期 LLM 驱动 GIS Agent 的概念验证。机构主页：https://giscience.psu.edu/llm-geo-an-open-source-autonomous-gis-prototype/ 【已查证】
- **架构摘要**：
  - 以 LLM 为**推理核心（reasoning core）**，利用其自然语言理解、推理与**代码生成**能力，自动完成空间数据采集、分析与可视化。【已查证】
  - 提出自主 GIS 的**五个自治目标**：self-generating（自生成）、self-organizing（自组织）、self-verifying（自校验）、self-executing（自执行）、self-growing（自成长）。【已查证，摘要页】
  - LLM-Geo 以 GPT-4 为"大脑"，支持自主空间推理、工作流规划、**代码生成与任务执行**；三个案例均返回聚合数字、图表与地图。【已查证，机构主页】
- **规划模式**：Plan-and-Execute + **代码生成执行**（LLM 写 Python/Geo 代码再跑）；空间计算由生成的代码（GeoPandas 等）完成。
- **记忆/状态 / 扩展**：通过"自成长"愿景扩展工具库；论文自承缺少日志记录与代码测试模块。【已查证，摘要页】
- **可借鉴点**：
  1. **五个自治目标**可作为 GeoMind Agent 能力清单的对标（尤其 self-verifying——空间计算结果要可校验）。
  2. LLM 生成"可执行工作流"而非直接给答案，工作流即产物、可审计。
- **不足/局限**：
  1. 原型早期，无日志、无代码沙箱测试，鲁棒性弱。
  2. 空间计算在 Python 进程内，未利用数据库内核/空间索引，大数据量下不可扩展。
- **对 GeoMind 的直接启发**：把"代码生成执行"升级为"**SQL/OGC 下推生成执行**"——LLM 不写 GeoPandas 脚本，而是生成带 R-Tree 空间索引命中、空间连接/聚合下推的 SQL，既保留 LLM-Geo 的自执行范式，又获得数据库级性能与结构化空间状态。

---

## 7. LlamaRoute（如存在）

- **核实结论**：**未检索到名为「LlamaRoute」的学术论文或开源项目**。检索仅命中 LLM-as-Router（模型路由）、Ollama 本地路由优化、EMNLP'25 Findings 的 LLMAP（LLM-Assisted Multi-Objective Route Planning，arXiv/ACL 2025.findings-emnlp.416）等**名称相近但并非 LlamaRoute** 的工作。【未找到/无法核实，禁止编造其架构】
- **可替代参照**：LLMAP（https://aclanthology.org/2025.findings-emnlp.416.pdf）用 LLM 理解人类指令并处理含 POI 位置/评分/评论数的地图数据做多目标路线规划；为规避 POI 过多导致的 token 超限，按 POI 类型采样 10 个 POI 输入 LLM。【已查证，PDF 片段】
- **对 GeoMind 的启发**：POI 数量大时不能全量塞进 prompt——应先用数据库空间/标量条件做**粗筛（下推到 OceanBase）**，只把少量候选 POI 交给 LLM 排序，避免 token 爆炸与幻觉。

---

## 8. Space2Graph（如存在）

- **核实结论**：**未检索到名为「Space2Graph」的学术论文或开源项目**。检索仅命中"时空知识图谱综述"（arXiv 2512.16487）、"网格投影构建空间知识图谱"（arXiv 2411.02309）、city2graph（地理关系转图做空间网络分析）等**空间+图方向的相邻工作**，无同名项目。【未找到/无法核实，禁止编造其架构】
- **相邻方向参考**：空间关系→图（实体为节点、空间关系为边）的建模思路，与"结构化空间状态"相关；city2graph 提供 GeoAI 空间网络分析的图表示管线。【已查证标题/摘要】
- **对 GeoMind 的启发**：候选 POI 之间的空间关系（邻接、可达、包含）不必每次重算，可在结构化空间状态里维护一张轻量"空间关系图"，供多跳推理复用。

---

## 9. 补充项目 A：MapAgent（分层多智能体 + 动态地图工具集成，2025）

- **名称 / 机构 / 时间**：MapAgent: A Hierarchical Agent for Geospatial Reasoning with Dynamic Map Tool Integration。Md Hasebul Hasan, Mahir Labib Dihan, Mohammed Eunus Ali, Md Rizwan Parvez（BUET / Monash / QCRI），2025-09。链接：https://arxiv.org/abs/2509.05933 ；开源 https://github.com/Hasebul/MapAgent 【已查证，HTML 全文】
- **架构摘要（重点参考）**：
  - **两层脚手架（Hierocracy）**：
    - **顶层 Planner 层**：Planner Agent 把用户查询 x 分解为结构化子目标序列 [g1…gn]，依据一份 **Module Inventory（模块清单）** 为每个子目标选模块，输出执行计划 π=[(g_i, m_i)]。
    - **底层 Execution 层**：含轻量模块与专门的 Map-Service 模块；Map-Service 由专属 **Map-Tool Agent** 管理。
  - **模块清单（Module Inventory）**：Visual Place Recognizer（从地图快照识别中心地点与半径）、Sequencer（把结果结构化排序）、Solution Generator（合成最终答案）、Answer Generator（提炼并校验一致性）、Map-Service（工具密集）。
  - **四个异构地图工具**：Nearby（周边 POI 搜索）、PlaceInfo（地点详情）、Route（两点路径/距离/时间）、Trip（组合 Place Details + Directions，做多点行程与路径）；每个工具由一个或多个 Google Maps 原生 API 封装，支持**并行 + 串行混合编排**。
- **规划模式**：**Plan-and-Execute 的分层多智能体**，刻意把 planning 与 execution 解耦；工具密集子目标再下沉给专门的 Map-Tool Agent，避免"扁平工具集"导致的 tool inflation（工具爆炸）与 tool incapability（工具太原始）。
- **记忆/状态 / 扩展**：模块即插件，按 Module Inventory 注册；支持多模态（地图截图 → Visual Place Recognizer）。消融实验：去掉分层架构准确率掉 27.18%，再去掉自研地图工具再掉 22%。【已查证】
- **可借鉴点**：
  1. **模块清单 + 计划-执行解耦**：GeoMind 应维护一份 Skill/Tool 注册表，Planner 只做"子目标→Skill"路由，不直接调底层 API。
  2. **把相近地图 API 聚合成 4 个高层工具（Nearby/PlaceInfo/Route/Trip）**，并行/串行混合编排——可直接映射为高德开放平台能力的插件化封装。
  3. **Answer Generator 专门做一致性校验**，对应 self-verifying。
- **不足/局限**：
  1. 依赖 Google Maps API，空间计算在 API 侧，无数据库/空间索引概念。
  2. 状态以中间消息传递，未沉淀为持久结构化空间状态。
- **对 GeoMind 的直接启发**：这是与 GeoMind 最对位的架构——采用「Planner（意图+约束）→ Skill 注册表（插件化）→ 空间计算下推 OceanBase + 高德 API」的分层；并把多模混合检索封装为 Nearby（空间）/Attribute（标量）/Semantic（向量）三个可组合 Skill。

---

## 10. 补充项目 B：CityGPT（城市 IoT 时空分析多智能体，2024）

- **名称 / 机构 / 时间**：CityGPT: Towards Urban IoT Learning, Analysis and Interaction with Multi-Agent System。Qinghua Guan, Jinhui Ouyang, Di Wu, Weiren Yu，2024-05-23。链接：https://arxiv.org/abs/2405.14691 【已查证】
- **架构摘要**：
  - **三类 + 融合共多个 Agent**：Requirement Agent（自然语言理解用户输入）→ 把分析任务分解为 temporal 与 spatial 两条线，由对应的 **Temporal Agent / Spatial Agent** 分别完成时序与空间分析 → **时空融合/可视化 Agent** 接收两路结果，调用子可视化 Agent 出图并生成文字描述。【已查证，摘要页】
- **规划模式**：角色化多智能体（需求理解 / 时序分析 / 空间分析 / 融合表达），按职责分工而非按 API 分工。
- **可借鉴点**：
  1. **空间分析与时间分析显式拆成两个 Agent**，再融合——GeoMind 的"可达性/客流热力"正是时空联合问题。
  2. 末端有专门的"表达 Agent"负责地图可视化 + 文字解释，对应高德"空间表达"层。
- **不足/局限**：面向 IoT 时序数据，非 POI 检索/选址；无数据库空间下推。
- **对 GeoMind 的直接启发**：Agent 分工可按"空间算子 / 标量过滤 / 向量语义 / 地图表达"四条职责线拆分，最后由表达层合成地图 + 结构化结论，对齐高德三层能力。

---

## 11. 补充项目 C：GeoAgent（MCTS + 代码解释器 + RAG 的地理数据分析 Agent，2024）

- **名称 / 机构 / 时间**：An LLM Agent for Automatic Geospatial Data Analysis（自称 GeoAgent）。Yuxing Chen, Weijie Wang, Sylvain Lobry, Camille Kurtz，2024-10-24。链接：https://arxiv.org/abs/2410.18792 【已查证】
- **架构摘要**：
  - 把**代码解释器（code interpreter）+ 静态分析（static analysis）+ 检索增强生成（RAG）**集成进 **蒙特卡洛树搜索（MCTS）**，用于地理空间数据处理的多步程序生成；并贡献了一个含单轮/多轮任务（数据获取、分析、可视化）的 benchmark。【已查证，摘要页】
  - 核心结论：仅靠 LLM 自身知识不足以准确完成地理任务编程，需要连贯多步过程与多次函数调用。【已查证】
- **规划模式**：MCTS 搜索 + 代码执行反馈（试错-验证），比 ReAct 更强调对候选执行轨迹的搜索与校验。
- **可借鉴点**：
  1. **静态分析 + 执行反馈做自我校验**：生成的 SQL/工具调用可先做语法/ schema 校验再执行。
  2. RAG 检索地理库/函数用法，缓解"冷门 GIS 库幻觉"——对应 GeoMind 把 OceanBase OGC 函数文档、高德 API schema 做 RAG。
- **不足/局限**：空间计算仍在 Python 地理库进程内；MCTS 推理成本高，竞赛实时性需权衡。
- **对 GeoMind 的直接启发**：LLM 生成 SQL 后，先用 schema/函数合法性做静态校验、再用小样本 dry-run 验证命中 R-Tree，避免把错误的空间计算下推到生产库。

---

## 跨案例对比表

| 项目 | 规划模式 | 工具调用方式 | 状态/记忆机制 | 扩展机制 | 核心借鉴点 |
|---|---|---|---|---|---|
| GeoGPT (2307.07930) | AutoGPT 式类 ReAct 思维链 | LangChain 串联成熟 GIS 工具（Buffer/Intersect/Erase） | 工具输出串接，无持久空间状态 | 注册更多 GIS 工具（"foundational+professional"） | 选址=链式空间算子，可映射为 OGC 函数下推 |
| UrbanGPT (2403.00813, KDD'24) | 非 Agent；时空编码器+指令微调 | 不调外部工具，时空依赖编码进表征 | 指令集覆盖多任务 | 指令微调扩展下游任务 | 城市要素带时空依赖，状态须含时间维 |
| DynamicRouteGPT (2408.14185) | 全局 Dijkstra + 局部 LLM 决策闭环 | 实时检索/备选路径/GPT 决策/动态调整四模块 | 实时交通与路口状态 | 换路网/偏好即可复用 | 确定性算路+LLM 偏好决策分层 |
| MapGPT-学术 (2310.11029) | 概念框架 | 空间计算+可视化 building blocks | 未实现 | 空间专用 tokenization/向量 | 空间向量作为一等公民 |
| TP-RAG/EvoRAG (2504.08694, EMNLP'25) | RAG 增强 LLM 规划 | 轨迹/POI 知识库检索 | 外部轨迹检索源 | 进化式融合多参考 | 检索需去冲突、做时空合理性校验 |
| AutoGIS/LLM-Geo (2305.06453) | Plan-and-Execute + 代码生成 | GPT-4 生成 Python/Geo 代码执行 | 早期，无日志 | 五自治目标（自成长）扩工具 | 自执行工作流 + self-verifying |
| MapAgent (2509.05933) | **分层多智能体：Planner/Execution 解耦** | 4 个聚合地图工具（Nearby/PlaceInfo/Route/Trip），并行+串行 | Module Inventory 中间态 | 模块插件化注册 | **与 GeoMind 最对位：Skill 注册表+计划执行解耦+答案校验** |
| CityGPT (2405.14691) | 角色化多智能体（需求/时序/空间/融合） | 时序与空间分析 Agent + 可视化子 Agent | 两分析结果融合 | 新增分析 Agent | 空间/时间分线再融合，末端地图表达 |
| GeoAgent (2410.18792) | **MCTS 搜索 + 代码解释器** | 代码解释器+静态分析+RAG | 执行反馈作为搜索奖励 | RAG 检索函数/库用法 | SQL 静态校验+dry-run，避免下推错误计算 |
| LlamaRoute | **未找到同名项目** | — | — | — | 参照 LLMAP：POI 先数据库粗筛再给 LLM |
| Space2Graph | **未找到同名项目** | — | — | — | 维护轻量空间关系图供多跳复用 |

---

## 对 GeoMind 的综合架构结论（落地建议）

1. **采用 MapAgent 的分层多智能体骨架**：顶层 Planner 做「意图理解 → 约束建立 → 子目标分解」，依据一份 **Skill 注册表**路由；底层按职责拆 Skill：空间算子（OceanBase OGC/R-Tree）、标量过滤、向量语义检索、高德 API（路径/实时）、地图表达。
2. **空间计算一律下推数据库内核**：借鉴 GeoGPT/AutoGIS 的"LLM 编排 + 专业空间计算"分工，但把 Python/GeoPandas 替换为 **OceanBase OGC 函数下推 SQL**（ST_Buffer / ST_Intersects / 空间连接 / 空间聚合），命中 R-Tree 索引；中间结果以 `SDO_GEOMETRY` 沉淀为**持续更新的结构化空间状态**。
3. **三模混合检索封装为可组合 Skill**：Nearby（空间）+ Attribute（标量）+ Semantic（POI 向量），对应 MapGPT 的"空间向量一等公民"与高德 POI 检索/选址场景。
4. **加 self-verifying 闭环**：借鉴 GeoAgent 的静态分析 + GeoGPT/AutoGIS 的可复现（temperature=0），生成 SQL 先做 schema/函数合法性校验与小样本 dry-run，再由独立 Answer/校验 Agent 做时空合理性检查（对齐高德 7 步闭环）。
5. **POI 大集先粗筛后精排**：借鉴 LLMAP 与 TP-RAG——先用数据库空间+标量条件下推粗筛出少量候选，再交 LLM 按语义向量/偏好排序，避免 token 爆炸与检索噪声。

---

## 参考链接清单（均已实际打开核实）

- GeoGPT 摘要页：https://arxiv.org/abs/2307.07930
- GeoGPT PDF：https://arxiv.org/pdf/2307.07930
- UrbanGPT 摘要页：https://arxiv.org/abs/2403.00813
- UrbanGPT 代码：https://github.com/HKUDS/UrbanGPT
- UrbanGPT ACM DOI：https://dl.acm.org/doi/abs/10.1145/3637528.3671578
- DynamicRouteGPT 摘要页：https://arxiv.org/abs/2408.14185
- PathGPT（个性化路径）：https://arxiv.org/pdf/2504.05846
- MapGPT-学术（空间数据）摘要页：https://arxiv.org/abs/2310.11029
- MapGPT-VLN（机器人导航，已排除）：https://arxiv.org/abs/2401.07314
- Mapbox MapGPT（商业）：https://www.mapbox.com/mapgpt
- TripGenie（Trip.com 商业产品）：https://www.trip.com/newsroom/introducing-tripgenie-groundbreaking-ai-travel-assistant/
- TP-RAG 摘要页：https://arxiv.org/abs/2504.08694
- TP-RAG EMNLP DOI：https://doi.org/10.18653/v1/2025.emnlp-main.626
- AutoGIS 摘要页：https://arxiv.org/abs/2305.06453
- LLM-Geo 原型（Penn State）：https://giscience.psu.edu/llm-geo-an-open-source-autonomous-gis-prototype/
- MapAgent 摘要/全文：https://arxiv.org/abs/2509.05933
- MapAgent 代码：https://github.com/Hasebul/MapAgent
- CityGPT 摘要页：https://arxiv.org/abs/2405.14691
- GeoAgent 摘要页：https://arxiv.org/abs/2410.18792
- LLMAP（路线规划相邻工作）：https://aclanthology.org/2025.findings-emnlp.416.pdf
- 时空知识图谱综述（Space2Graph 相邻方向）：https://arxiv.org/abs/2512.16487

> 未找到/无法核实项：**LlamaRoute**、**Space2Graph** 两个名称未检索到对应学术项目，已在正文明确标注，未做任何架构编造。
