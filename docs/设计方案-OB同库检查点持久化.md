# 设计方案：LangGraph 检查点同库持久化 OBCheckpointer（任务卡 3）

> 架构出处：§2.2 L5「检查点：LangGraph checkpointer **同库持久化**，免费获得会话记忆 / 故障恢复 / HITL」。
> 现状：`backend/brain/graph.py` 用模块级 `MemorySaver()`，后端进程重启即丢会话与 interrupt 态。
> 本卡后：追问 → **换进程** → 补答 → resume 仍能续跑。

## 1. 接口对齐：以已安装版本实测为准（不凭文档记忆）

安装版本 `langgraph 1.2.15` / `langgraph-checkpoint 4.3.0`。用包装 `MemorySaver` 的探针跑通
「interrupt → resume」全链（脚本 `.workbuddy/tmp/probe_checkpoint_api.py`），实测到的事实：

| 项 | 实测结论 |
|---|---|
| 必须实现的抽象方法 | `get_tuple / list / put / put_writes / delete_thread`（`get` 由基类委派 `get_tuple`） |
| `config["configurable"]` 键 | `thread_id`、`checkpoint_ns`、`checkpoint_id`（put 时是**父** id），另有内部键 `__pregel_runtime` |
| `checkpoint_ns` | 本图恒为 `''`（无子图） |
| `put` 返回 | `{"configurable": {thread_id, checkpoint_ns, checkpoint_id}}` |
| `checkpoint_id` 形态 | uuid6 字符串（如 `1f1c9220-fe8d-…`）；**字典序 == 创建序**（探针断言 `sorted(ids) == ids` 为 True）→ 「取最大即最新」与「倒序 list」成立 |
| serde | 基类自带 `JsonPlusSerializer`，`dumps_typed()` 返回 `(type, bytes)` 二元组，实测 type 为 **`msgpack`**（不是 JSON 文本）→ 表里必须存 `type` + 二进制列 |
| `put_writes` 入参 | `writes` 是 `(channel, value)` 序列；`task_path` 实测形如 `~__pregel_pull, intent`，resume 哨兵写入为 `task_id='00000000-…'`、`task_path=''` |
| 写入序号 | `idx = enumerate(writes)` 的位序；`WRITES_IDX_MAP = {__error__:-1, __scheduled__:-2, __interrupt__:-3, __resume__:-4}` 是**特殊通道的固定负索引**，参考实现里负索引允许覆盖、正索引重放时跳过 |
| `pending_writes` 顺序 | 契约要求按 `writes_sort_key(task_path, task_id, idx)` 有序返回 |
| `get_delta_channel_history` | 基类默认实现只依赖 `get_tuple` + `parent_config` + `pending_writes` 顺序 → **不需要覆写**（`InMemorySaver` 那套是为性能） |

## 2. 范围

做：002 迁移两张新表、`OBCheckpointer`、`GEOMIND_CHECKPOINT_BACKEND` 开关（memory|ob）、
测试默认 memory、跨实例与跨进程验收。**不动** 7 节点骨架与任何节点体，不改已有表（§5.6）。

## 3. 设计决定

**D1 整份 checkpoint 存一个 blob，不按通道拆版本。** 卡片钉死两张表
（`graph_checkpoint` / `graph_checkpoint_writes`），所以 `put` 时把**含 `channel_values` 的整个
checkpoint** 用 `self.serde.dumps_typed()` 打成一份 `(type, blob)` 存进 LONGBLOB，
`get_tuple` 原样还原。代价是要说清的：每个 checkpoint 都带全量状态，空间换实现复杂度。
可行性论证两条：① 基类的 delta 历史默认实现只沿 `parent_config` 走链并看
`channel_values[ch]` 是否填充，全量 blob 下每个祖先都填充 → 终止条件天然满足，语义不变；
② 本图通道都是普通 last-value（实测 channel 集合就是 `BrainState` 那些键 + `branch:to:*`），
没有按通道增量需求。体积实测值记进 §6。

**D2 `metadata` 走基类的 `get_checkpoint_metadata(config, metadata)` 再序列化**，
不自己拼字段（它负责把 `step/source/parents/run_id` 等规整成契约形态）。

**D3 PK 不含 `checkpoint_ns`（照卡片），但必须守卫。** 本图 `checkpoint_ns` 恒空；
若将来接入子图，非空 ns 的不同命名空间会**静默撞同一行**。因此
`_ns(config)` 在 ns 非空时直接抛 `CheckpointStorageError`——宁可炸，不静默串数据。
（这是把卡片的简化假设变成显式约束，属可测的偏差，见 §5 偏差请示。）

**D4 writes 表比卡片多一列 `task_path VARCHAR(255)`。** 契约要求 `pending_writes` 按
`writes_sort_key(task_path, task_id, idx)` 排序，不落 `task_path` 就只能按 `(task_id, idx)` 排，
是**降级实现**；多存一列成本为零（卡片只钉了 PK，不含此列不构成冲突，PK 保持
`(thread_id, checkpoint_id, task_id, idx, channel)`）。

**D5 put_writes 的幂等按参考语义分两条路。** 正索引（普通通道）重复写视为同一逻辑值，
用 `INSERT IGNORE`（跳过）；负索引（`__interrupt__`/`__resume__`/`__error__`/`__scheduled__`）
必须能覆盖，用 `ON DUPLICATE KEY UPDATE`。实测一轮 interrupt+resume 会对同一 checkpoint
发生两次写入（哨兵 task 与真实 task），不区分就会把 pending 的 `__interrupt__` 弄丢或弄重。

**D6 `list` 的 `filter` 在 Python 侧过滤。** metadata 是二进制 blob，库里做等值过滤要额外
冗余列；`list` 只在本卡用于时间旅行/调试与测试断言，按 `checkpoint_id` 倒序取回后在内存筛。
（`before` 与 `limit` 下推到 SQL。）

**D7 写路径失败一律抛，不静默降级。** `OBConnection.execute` 已把 `pymysql.Error` 包成
`OBError`，本模块直接透传并补一句语义：checkpoint 丢失比一次请求失败更危险。
读路径（`get_tuple` 无数据）返回 `None` 是正常的「无 checkpoint」。

**D8 开关只作用在单例构造处。** `graph.py` 模块级 `_CHECKPOINTER` 依
`get_settings().checkpoint_backend` 选 `MemorySaver` 或 `OBCheckpointer`，
**保持"只构造一次"的模式不变**（踩坑 #8：每次 `build_graph` 新建会拿到空 checkpoint，
resume 直接 `KeyError`）。节点与路由层不感知后端类型。默认 `memory`，生产/验收用
`GEOMIND_CHECKPOINT_BACKEND=ob`；`tests/conftest.py` 在任何 `Settings` 实例化前强制 memory
（仿已有的 fake LLM 两行写法），这样没建 002 表的环境也能跑全量测试。

**D9 `OBCheckpointer` 复用 `OBConnection` 连接池**，不自建 pymysql 连接（§2.2 L6 单一访问入口）。
并发安全依赖连接池本身（每次取还一条连接），不引入模块级游标。

## 4. 落点

| 文件 | 动作 |
|---|---|
| `backend/db/migrations/002_add_graph_checkpoint.sql` | 两张新表（`CREATE TABLE IF NOT EXISTS`），头注写实测硬约束 |
| `backend/db/checkpoint_ob.py` | 新建 `OBCheckpointer` + `CheckpointStorageError` + `make_checkpointer()` 工厂 |
| `backend/config.py` | `checkpoint_backend: Literal["memory","ob"] = "memory"` |
| `backend/brain/graph.py` | `_CHECKPOINTER = make_checkpointer(...)`（仍是模块级单例） |
| `.env.example` | 新增 `GEOMIND_CHECKPOINT_BACKEND` 说明 |
| `tests/conftest.py` | 强制 memory 后端 |
| `tests/test_checkpoint_ob.py` | round-trip / pending_writes / list / delete_thread / 跨实例 resume |

## 5. 验收标准（全部可测）

- [x] 序列化 round-trip 跨实例：实例 A `put` → 新建实例 B `get_tuple` → checkpoint/metadata 等价（模拟进程重启）。
- [x] `put_writes` 后 `get_tuple` 带 `pending_writes`，且顺序符合 `writes_sort_key`。
- [x] `list` 倒序、`before` 生效、`limit` 生效、`filter` 命中与不命中各一例。
- [x] `delete_thread` 后两张表该 thread 均为 0 行（**从库里读回计数**，不看返回值）。
- [x] 特殊通道负索引可覆盖、正索引重放跳过（幂等）。
- [x] `db_ok` 集成：真图跑 interrupt → 全新 `OBCheckpointer` 实例 resume → 出候选（跨实例恢复）。
- [x] 进程级验收：起服 → 制造 interrupt → 带 `GEOMIND_CHECKPOINT_BACKEND=ob` **重启进程** → HTTP resume → 200 且出结果。
- [x] `test_interrupt_then_resume` 在 memory 后端仍绿；全量 pytest + ruff 0 + mypy 0。
- [x] 002 迁移 mysql 实跑 0 错误，`SHOW CREATE TABLE` 回读确认 LONGBLOB 与 PK。

## 6. 风险与待量测

| 项 | 处置 |
|---|---|
| 全量 blob 的空间成本 | 实测单份 checkpoint 体积与一轮 8 个 checkpoint 的累计值，写进 §8 证据表；超量再考虑按通道拆存（属下一卡） |
| msgpack 里 tuple→list | 跨进程还原后 `spec["center"]` 由 tuple 变 list；下游 Pydantic `tuple[float, float]` 会强制转回，集成用例必须实测断言这一点 |
| SeekDB 对 `INSERT IGNORE` / `ON DUPLICATE KEY UPDATE` 的兼容 | 迁移实跑后用一条最小写入用例验证（不是假设 MySQL 语义一定全等价） |
| LONGBLOB 二进制回读 | 用 `bytes` 直接回读比对原 blob 长度，防止字符集转换吃掉字节 |
| 时间戳列 | 与 001 同口径（服务端 `SYSDATE(6)` 风格），不用客户端时钟 |

## 7. 实施顺序

读安装版接口（已完成，见 §1 表）→ 002 迁移实跑 → `checkpoint_ob.py` → 开关与单例 →
测试（含跨实例/跨进程）→ 进程级验收 → 文档与 commit。

## 8. 实施结果（2026-10-16）

### 8.1 验收对照

| 项 | 结果 |
|---|---|
| 002 迁移实跑 | ⚠️→✅ 第一次跑出 **ERROR 1064**：`blob` 不能裸作列名（SeekDB 保留字）→ 改列名 `value_blob` 并在文件头记为第 5 条实测约束；第二次实跑 0 错误。`information_schema` 回读确认两表 8/8 列、`checkpoint`/`metadata`/`value_blob` 均为 **longblob** |
| round-trip 跨实例 | ✅ 实例 A `put` → **新建实例 B** `get_tuple` → `channel_values` 与 metadata 等价、`pending_writes==[]`、`parent_config is None` |
| pending_writes | ✅ 三条写入按 `writes_sort_key` 还原（空 `task_path` 的 resume 哨兵排最前），值原样回 |
| list 倒序 / before / limit / filter | ✅ 倒序 `[c3,c2,c1]`；`before=c3` → `[c2,c1]`（不含基准点）；`limit=1` → `[c3]`；metadata `{"step":3}` 命中、`{"step":99}` 空 |
| delete_thread | ✅ 断言方式是从库里 `SELECT COUNT(*)` 读回两张表都 **0 行**（不看返回值脸色），且 `get_tuple` 返回 None |
| 写语义分叉 | ✅ 特殊通道（负索引）可覆盖、普通通道（正索引）先到先得（INSERT IGNORE），两条各一条用例 |
| 守卫 | ✅ 非根 `checkpoint_ns` 抛 `CheckpointStorageError`（宁可炸不静默撞行）；缺 `thread_id` 同样抛；写失败 `OBError` 原样透传（不降级） |
| 二进制保真 | ✅ 与 `serde.dumps_typed` 产出的原始 bytes **逐字节相等**且 `LENGTH()` 一致（无字符集吃字节） |
| 集成：换实例恢复 HITL | ✅ 真图 `interrupt` → 换成全新 `OBCheckpointer` → `resume_query("春熙路")` → 出候选 + `isochrone.degraded=True` + 图层声明 |
| **进程级验收**（换真进程） | ✅ 见 §8.3：`GEOMIND_CHECKPOINT_BACKEND=ob` 起服 → 制造 interrupt → **杀进程重启** → HTTP `/resume` → `interrupted:false`、**10 个候选**、决策句、图层 kinds `['poi','heatmap','isochrone']`，随后 `layers/l_iso` 返回 13 点闭合环（`degraded:false, sampled_points:60`） |
| 质量门 | ✅ pytest **159 passed**（本卡 +16）+ ruff 0 + mypy 43 文件 0；变异检查 **9/9 全被抓住**（取最旧、before 用 <=、特殊通道不覆盖、普通通道被覆盖、不按 sort key 排、ns 守卫失效、thread_id 守卫失效、版本号不递增、漏删 writes） |

### 8.2 实跑纠偏的三条（都不是先验知道的）

1. **`blob` 是 SeekDB 保留字**，作列名直接 1064。DDL 里改 `value_blob`，同时卡片给的列名在文档与代码里都对不上——属可见偏差（§8.4）。
2. **`uuid6()` 返回 UUID 对象**，而框架与库里存的是 `str`。测试一开始直接拿对象比较，`parent_config` 那步炸出 `UUID('1f1c…') != '1f1c…'`；改成 `str(uuid6())` 后与生产一致。
3. **基类对 `str` 版本号抛 `NotImplementedError`**：`BaseCheckpointSaver[str]` 必须自己实现 `get_next_version`，否则图跑不起来。本实现用 32 位零填充十进制串（字典序 == 数值序），实测 `v1<v2<v3` 且 `…09 → …10` 正确进位。

另外实测两条事实：序列化器 `dumps_typed` 的类型标签是 **`msgpack`**（故 `type` 列必要）；**tuple 会被降级成 list**（`JsonPlusSerializer` 行为）。后者对本项目无害——跨进程还原后进的都是 Pydantic `tuple[float, float]` 入参，会被强制转回；已由 `test_serde_fidelity_documents_container_types` 钉成断言而不是注释。

### 8.3 进程级验收复现命令序列

```bash
# 1) 起服（ob 后端）
GEOMIND_CHECKPOINT_BACKEND=ob D:/python_env/geomind/Scripts/python.exe -m uvicorn \
  "backend.api.routes:create_app" --factory --host 127.0.0.1 --port 8000
# 2) 建会话 + 发一条没有地名的查询 → 触发 HITL 追问（interrupt 态此刻在库里）
SID=$(curl -s -X POST localhost:8000/api/sessions -H 'Content-Type: application/json' \
      -d '{"scene":"site_selection"}' | python -c "import sys,json;print(json.load(sys.stdin)['session_id'])")
curl -s -X POST localhost:8000/api/sessions/$SID/chat -H 'Content-Type: application/json' \
      -d '{"query":"找个安静能自习的咖啡馆"}'      # → interrupted: true
# 3) 杀掉后端进程，再用同样的环境变量重新拉起一个**全新进程**
# 4) 补答 → 必须从中断处续跑出结果
curl -s -X POST localhost:8000/api/sessions/$SID/resume -H 'Content-Type: application/json' \
      -d '{"answer":"春熙路"}'                    # → interrupted: false + 10 候选 + 3 图层
curl -s localhost:8000/api/sessions/$SID/layers/l_iso   # → 13 点闭合环 Polygon
```

实测体积（同一会话 9 个 checkpoint）：`graph_checkpoint` 9 行、`graph_checkpoint_writes` 21 行、
`SUM(OCTET_LENGTH(checkpoint)) = 35,423 B` → **约 3.9 KB/个 checkpoint、35 KB/会话轮**。

### 8.4 与卡片的偏差（请示）

| 卡片 DDL | 实际落地 | 原因 |
|---|---|---|
| writes 表列 `blob` | `value_blob` | SeekDB 保留字，实跑 1064 |
| writes 无 `task_path` | 加 `task_path VARCHAR(255)` | `get_tuple` 契约要求按 `writes_sort_key(task_path, task_id, idx)` 有序，不落此列只能降级排序 |
| 主表 `metadata LONGBLOB`（无类型列） | 加 `metadata_type VARCHAR(16)` | serde 是 `(type, bytes)` 二元组，metadata 同样需要类型标签 |

三处都是**加列/改名**，未减字段、未改 PK 语义，属"只新增"范围内。

### 8.5 遗留

- `delete_for_runs` / `copy_thread` / `prune` 未实现（基类默认抛 `NotImplementedError`）；本图无这两种用法，将来做「会话清理策略」时再补。
- 检查点表**没有 TTL/清理**：按 §8.3 的 35 KB/会话轮，1 万轮约 350 MB，长跑演示环境需要一条清理任务（`prune`/按 `created_at` 批量删）。
- 多 worker（uvicorn --workers>1）并发写同一 thread 未做压测；当前演示形态是单 worker。
- 前端目前不消费「跨进程恢复」这一能力（会话仍在内存里），HITL 恢复路径的 UI 呈现属卡 6 的约束编辑闭环。

