-- =============================================================================
-- GeoMind 项目正式 DDL · V1.4.0 SeekDB 实测口径
-- 文件：backend/db/migrations/001_init_schema.sql
-- 依据：docs/压测报告-10万级.md（2026-10-15 实跑）+ docs/SeekDB环境实测报告.md
--
-- 三条硬约束（违反必报错，写 SQL 前先看）：
--   1. 空间列一律 SRID 0 + WGS84 经纬度存值：写 `SRID 4326` 报 ERROR 3548，
--      `CREATE SPATIAL REFERENCE SYSTEM` 报 ERROR 1064（两条路都堵死）。
--   2. 不得出现 REPLICA_NUM / ORGANIZATION INDEX / TABLET_SIZE / USE_BLOOM_FILTER
--      / BLOCK_SIZE；向量索引 LIB=VSAG 省略（默认即 vsag）。
--   3. SPATIAL INDEX 在 V1.4 完全不参与查询计划（7 种谓词 + INDEX hint 全部
--      TABLE FULL SCAN，MBR* 函数不存在）。因此：
--        · 空间过滤必须走「lon/lat 冗余列 + KEY(lon,lat)」MBR 预筛，再用 ST_* 精确复核；
--        · 空间连接必须走「grid_id 归属冗余列 + 普通索引」JOIN，不要用 ST_Contains 连接；
--        · SPATIAL KEY 仍保留（成本仅存储、未来内核修复后可期），但查询路径不得依赖它。
--
-- 单位陷阱：ST_Distance_Sphere 返回米；ST_Distance / _ST_Dwithin / ST_Buffer 参数是度。
-- =============================================================================

CREATE DATABASE IF NOT EXISTS geomind;
USE geomind;

-- ---------------------------------------------------------------------------
-- 类目维表
-- ---------------------------------------------------------------------------
CREATE TABLE dim_category (
  category_id   INT          PRIMARY KEY,
  name_l1       VARCHAR(50),
  name_l2       VARCHAR(50),
  name_l3       VARCHAR(50),
  KEY idx_cat_l1 (name_l1)
);

-- ---------------------------------------------------------------------------
-- POI 主表（五模齐全：标量 + 空间 + 向量 + 全文 + JSON）
-- ---------------------------------------------------------------------------
CREATE TABLE dim_poi (
  poi_id        BIGINT       PRIMARY KEY,
  name          VARCHAR(200) NOT NULL,
  category_id   INT          NOT NULL,
  brand         VARCHAR(100),
  address       VARCHAR(300),
  city          VARCHAR(50),
  location      POINT NOT NULL,                    -- SRID 0，WGS84 经纬度
  lon           DOUBLE,                            -- 冗余：MBR 预筛左边界（与 location 同源，ETL 或 ST_X 回填）
  lat           DOUBLE,                            -- 冗余：MBR 预筛下边界
  grid_id       BIGINT,                            -- 冗余：网格归属，替代 ST_Contains 连接
  rating        DECIMAL(3,1) DEFAULT 0,
  attrs         JSON,                              -- 营业/停车/人均等灵活属性
  status        TINYINT      NOT NULL DEFAULT 1,   -- 1=营业中
  emb           VECTOR(1024),                      -- BGE-m3，V1.4 已实测 1024 维
  -- 以下三个 KEY 是查询真正会走的路径
  KEY idx_poi_bbox (lon, lat),                     -- MBR 预筛（圈选 / KNN，实测 <1 ms）
  KEY idx_poi_grid (grid_id),                      -- 网格归属 JOIN（实测 20 ms）
  KEY idx_poi_cat_stat_rating (category_id, status, rating),
  -- 以下三个 KEY 为模态索引；SPATIAL KEY 当前不参与计划，保留待内核修复
  SPATIAL KEY idx_poi_loc (location),
  VECTOR KEY idx_poi_emb (emb) WITH (DISTANCE=COSINE, TYPE=HNSW, M=16, EF_CONSTRUCTION=200, EF_SEARCH=64),
  FULLTEXT KEY ft_poi_name (name) WITH PARSER ngram PARSER_PROPERTIES=(ngram_token_size=2)
);

-- ---------------------------------------------------------------------------
-- 事实表：门店 / 候选铺位
-- ---------------------------------------------------------------------------
CREATE TABLE fact_store (
  store_id         BIGINT       PRIMARY KEY,
  name             VARCHAR(200),
  rent             DECIMAL(10,2),
  area_sqm         DECIMAL(8,1),
  streetfront_tier CHAR(4),                        -- 临街档位 W1-W4
  location         POINT NOT NULL,
  lon              DOUBLE,
  lat              DOUBLE,
  grid_id          BIGINT,
  attrs            JSON,
  emb              VECTOR(1024),
  KEY idx_store_bbox (lon, lat),
  KEY idx_store_grid (grid_id),
  SPATIAL KEY idx_store_loc (location),
  VECTOR KEY idx_store_emb (emb) WITH (DISTANCE=COSINE, TYPE=HNSW, M=16, EF_CONSTRUCTION=200, EF_SEARCH=64)
);

-- ---------------------------------------------------------------------------
-- 事实表：客流网格（空间抽象层）
-- 网格规格（样例，固化在 ETL 与文档中）：
--   原点 (103.85, 30.45)，边长 0.01 度（约 1 km），50 列 × 40 行 = 2000 个网格
--   grid_id = FLOOR((lat - 30.45)/0.01) * 50 + FLOOR((lon - 103.85)/0.01) + 1
-- ---------------------------------------------------------------------------
CREATE TABLE fact_traffic_grid (
  grid_id       BIGINT,
  time_bucket   VARCHAR(32),                       -- workday_evening / weekend_noon ...
  grid_poly     POLYGON NOT NULL,                  -- SRID 0，WGS84 经纬度
  center_lon    DOUBLE,
  center_lat    DOUBLE,
  resident_cnt  INT,
  flow_cnt      INT,
  PRIMARY KEY (grid_id, time_bucket),
  KEY idx_grid_time (time_bucket),
  SPATIAL KEY idx_grid_poly (grid_poly)
);

-- ---------------------------------------------------------------------------
-- 会话状态快照（Spatial State 持久化）
-- ---------------------------------------------------------------------------
CREATE TABLE scene_solution (
  session_id    VARCHAR(64) PRIMARY KEY,
  scene         VARCHAR(64),
  version       INT,
  state         JSON,                              -- 完整 Spatial State
  created_at    DATETIME,
  updated_at    DATETIME ON UPDATE CURRENT_TIMESTAMP,
  KEY idx_scene_updated (scene, updated_at)
);

-- ---------------------------------------------------------------------------
-- 知识库切片（长期记忆，复用同一套多模检索）
-- ---------------------------------------------------------------------------
CREATE TABLE knowledge_doc (
  doc_id        BIGINT PRIMARY KEY,
  title         VARCHAR(200),
  text          TEXT,
  meta          JSON,
  emb           VECTOR(1024),
  VECTOR KEY idx_kb_emb (emb) WITH (DISTANCE=COSINE, TYPE=HNSW, M=16, EF_CONSTRUCTION=200, EF_SEARCH=64),
  FULLTEXT KEY ft_kb_text (text) WITH PARSER ngram PARSER_PROPERTIES=(ngram_token_size=2)
);

-- =============================================================================
-- 冗余列的一致性维护约定
--   · 写入路径：ETL 侧直接写 lon/lat/grid_id（推荐，零额外开销）；
--   · 兜底回填（库内完成，不违反"空间计算不下放 LLM"铁律，10 万行实测 906 ms）：
--       UPDATE dim_poi
--          SET lon = ST_X(location),
--              lat = ST_Y(location),
--              grid_id = CAST(FLOOR((ST_Y(location) - 30.45)/0.01) AS SIGNED) * 50
--                      + CAST(FLOOR((ST_X(location) - 103.85)/0.01) AS SIGNED) + 1
--        WHERE lon IS NULL;
--   · 校验（应恒为 0 行）：
--       SELECT COUNT(*) FROM dim_poi
--        WHERE lon IS NULL OR lat IS NULL OR grid_id IS NULL
--           OR ABS(lon - ST_X(location)) > 1e-9 OR ABS(lat - ST_Y(location)) > 1e-9;
-- =============================================================================
