-- GeoMind 10 万级压测 DDL（SeekDB V1.4 / OceanBase 4.3.5.3）
-- 硬约束：① 一律 SRID 0（SRID 4326 列级报 3548、注册语句报 1064）
--         ② 禁 REPLICA_NUM 等分布式选项（库级/表级均 ERROR 1064）
--         ③ 向量索引 LIB=VSAG 省略（默认即 vsag）
-- 目标：单表承载五模——标量 + 空间(POINT) + 向量(1024维) + 全文(ngram) + JSON

DROP DATABASE IF EXISTS geomind_bench;
CREATE DATABASE geomind_bench;
USE geomind_bench;

CREATE TABLE t_bench_poi (
  id        BIGINT PRIMARY KEY,
  name      VARCHAR(128) NOT NULL,
  category  INT NOT NULL,
  status    TINYINT NOT NULL DEFAULT 1,
  rating    DECIMAL(3,1) DEFAULT 0.0,
  city_code VARCHAR(8) NOT NULL,
  props     JSON,
  location  POINT NOT NULL,
  emb       VECTOR(1024),
  SPATIAL KEY idx_loc (location),
  VECTOR  KEY idx_emb (emb) WITH (DISTANCE=COSINE, TYPE=HNSW, M=16, EF_CONSTRUCTION=200, EF_SEARCH=64),
  FULLTEXT KEY idx_ft  (name) WITH PARSER ngram,
  KEY idx_cat    (category, status),
  KEY idx_rating (rating)
);

-- 100 个网格（10x10，覆盖成都主城范围），用于空间连接压测
CREATE TABLE t_bench_grid (
  grid_id INT PRIMARY KEY,
  cell    POLYGON NOT NULL,
  SPATIAL KEY idx_cell (cell)
);
