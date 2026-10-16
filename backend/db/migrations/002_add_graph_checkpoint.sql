-- =============================================================================
-- GeoMind 迁移 002 · LangGraph 检查点同库持久化（架构 §2.2 L5「同库持久化」）
-- 文件：backend/db/migrations/002_add_graph_checkpoint.sql
-- 依据：docs/设计方案-OB同库检查点持久化.md §1（langgraph 1.2.15 安装版实测）
--
-- 只新增两张表，不改 001 的任何表（§5.6 核心冻结）。
--
-- 四条实测约束：
--   1. checkpoint_id 是 uuid6 字符串，**字典序 == 创建序**（探针实测 sorted==created），
--      因此「取最新」用 MAX(checkpoint_id)，`list` 用 ORDER BY checkpoint_id DESC，
--      不依赖 created_at 的精度与时钟。
--   2. serde 是 JsonPlusSerializer，dumps_typed() 返回 (type, bytes)，实测 type='msgpack'
--      → 必须 varchar 存类型标签 + LONGBLOB 存字节，不能当文本存。
--   3. 本图 checkpoint_ns 恒为 ''（无子图），故 PK 不含 ns；OBCheckpointer 对
--      非空 ns 直接抛错，避免不同命名空间静默撞同一行。
--   4. 写序号：普通通道 idx 为正（重放跳过 = INSERT IGNORE），
--      特殊通道 __error__/__scheduled__/__interrupt__/__resume__ 为固定负索引
--      （必须可覆盖 = ON DUPLICATE KEY UPDATE），二者共用一张 writes 表。
--   5. 列名不得用 `blob`：SeekDB 解析器实测报 ERROR 1064（BLOB 是保留字，
--      不能裸作列名），已改用 `value_blob`。
--
-- 执行方式与 001 相同（PowerShell）：
--   & "D:\mysql\mysql-8.4.8-winx64\bin\mysql.exe" -h 127.0.0.1 -P 2881 -uroot --ssl-mode=DISABLED --force `
--     -e "source E:/2026ob_projects/GeoMind/backend/db/migrations/002_add_graph_checkpoint.sql"
-- =============================================================================

USE geomind;

-- ---------------------------------------------------------------------------
-- 检查点主表：一行 = 一个 checkpoint（整份状态，含 channel_values）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS graph_checkpoint (
  thread_id            VARCHAR(64)  NOT NULL,       -- = 会话 sid（run_query 的 thread_id）
  checkpoint_id        VARCHAR(36)  NOT NULL,       -- uuid6，字典序即时间序
  parent_checkpoint_id VARCHAR(36),                 -- 前驱；根为 NULL
  type                 VARCHAR(16)  NOT NULL,       -- serde 类型标签（实测 msgpack）
  checkpoint           LONGBLOB     NOT NULL,       -- dumps_typed(checkpoint) 的字节
  metadata_type        VARCHAR(16)  NOT NULL,
  metadata             LONGBLOB     NOT NULL,       -- dumps_typed(get_checkpoint_metadata(...))
  created_at           DATETIME(6)  DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (thread_id, checkpoint_id),
  KEY idx_ckpt_thread_created (thread_id, checkpoint_id)
);

-- ---------------------------------------------------------------------------
-- 中间写入表：一行 = 某 checkpoint 上某任务写的一个通道
-- put_writes 丢了它，interrupt 的 pending writes 就找不回，resume 必错
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS graph_checkpoint_writes (
  thread_id     VARCHAR(64)   NOT NULL,
  checkpoint_id VARCHAR(36)   NOT NULL,
  task_id       VARCHAR(36)   NOT NULL,
  idx           INT           NOT NULL,              -- 普通通道为正，特殊通道为负
  channel       VARCHAR(64)   NOT NULL,
  task_path     VARCHAR(255)  NOT NULL DEFAULT '',   -- 参与 writes_sort_key 排序
  type          VARCHAR(16)   NOT NULL,
  value_blob    LONGBLOB      NOT NULL,              -- 列名不能叫 blob（SeekDB 保留字，实测 1064）
  PRIMARY KEY (thread_id, checkpoint_id, task_id, idx, channel),
  KEY idx_writes_ckpt (thread_id, checkpoint_id)
);

-- ---------------------------------------------------------------------------
-- 校验（实跑后应看到两张表 + LONGBLOB 列）
--   SHOW TABLES LIKE 'graph_checkpoint%';
--   DESC graph_checkpoint;
--   SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS
--     WHERE TABLE_SCHEMA='geomind' AND TABLE_NAME IN ('graph_checkpoint','graph_checkpoint_writes');
-- ---------------------------------------------------------------------------
