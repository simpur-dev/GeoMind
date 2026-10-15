# -*- coding: utf-8 -*-
"""
生成生产库验证脚本：backend/etl/data/verify_prod.sql

把查询向量注入模板，产出可直接 `source` 的 SQL，覆盖：
  · 四模 / 五模混合检索（全文 + 向量 + 空间 + 标量 [+ JSON]）与 EXPLAIN
  · 空间圈选与 KNN：纯 ST_* 基线 vs MBR 预筛 的耗时对比
  · 空间连接：ST_Contains JOIN vs grid_id 冗余列 JOIN 的耗时对比
  · 知识库混合检索（全文 + 向量）
  · Spatial State 读写与增量更新
  · 冗余列一致性校验

计时方法（踩坑后定型，勿改回）：
  · 用 SYSDATE(6) 而非 NOW(6)：NOW(6) 在 OB 返回语句开始时刻，短查询会算出负值（实测出现过 -0.164ms）。
  · 每个计时查询重复 5 次，输出 min_ms 与 avg_ms：刚导入数据后 OB 后台在做合并，
    单次测量波动可达 3 倍（同一查询单次 7.8ms、三次平均却 24.2ms）；min 更接近真实能力。
  · 仍含客户端往返开销（约 1~3 ms 量级），<5 ms 的差异只作趋势参考。

用法：python backend/etl/gen_verify.py
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# 春熙路商圈中心与 1.5 km 半径（0.0135 度 ≈ 1.5 km）
C_LON, C_LAT = 104.0817, 30.6570
R_DEG = 0.0135
X0 = round(C_LON - R_DEG, 6)
X1 = round(C_LON + R_DEG, 6)
Y0 = round(C_LAT - R_DEG, 6)
Y1 = round(C_LAT + R_DEG, 6)

# 春熙路周边 5×5 网格（row 18~22, col 21~25），用于空间连接对比
GIDS = [r * 50 + c + 1 for r in range(18, 23) for c in range(21, 26)]
GID_LIST = ",".join(str(g) for g in GIDS)

REPEAT = 5


def timed(label, sql, n=REPEAT):
    """生成 n 次重复执行 + min/avg 输出。sql 不含结尾分号。"""
    out = []
    for i in range(1, n + 1):
        out.append("SET @t%d = SYSDATE(6);" % i)
        out.append(sql + ";")
        out.append("SET @d%d = TIMESTAMPDIFF(MICROSECOND, @t%d, SYSDATE(6));" % (i, i))
    vs = ",".join("@d%d" % i for i in range(1, n + 1))
    s = "+".join("@d%d" % i for i in range(1, n + 1))
    # 注意：LEAST() 在只有 1 个参数时报 ERROR 1582（预热块 n=1 时踩过）
    if n == 1:
        out.append("SELECT '%s' AS q, @d1/1000.0 AS min_ms, @d1/1000.0 AS avg_ms;" % label)
    else:
        out.append("SELECT '%s' AS q, LEAST(%s)/1000.0 AS min_ms, (%s)/%d000.0 AS avg_ms;"
                   % (label, vs, s, n))
    return "\n".join(out)


def read_vec(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return fh.read().strip()


def main():
    qvec = read_vec("qvec_coffee_quiet.txt")
    qvec_kb = read_vec("qvec_kb.txt")

    # ---- 计时用的 SQL 片段（与展示查询语义一致） ----
    Q1A = ("SELECT COUNT(*) FROM dim_poi "
           "WHERE status = 1 AND category_id = 1 AND rating >= 4.0 "
           "AND lon BETWEEN {X0} AND {X1} AND lat BETWEEN {Y0} AND {Y1} "
           "AND _ST_Dwithin(location, ST_GeomFromText('POINT({CLON} {CLAT})'), {RDEG}) "
           "AND MATCH(name) AGAINST ('+咖啡 +自习' IN BOOLEAN MODE)").replace(
               "{X0}", str(X0)).replace("{X1}", str(X1)).replace("{Y0}", str(Y0)).replace(
               "{Y1}", str(Y1)).replace("{CLON}", str(C_LON)).replace("{CLAT}", str(C_LAT)).replace(
               "{RDEG}", str(R_DEG))

    Q1B = ("SELECT COUNT(*) FROM dim_poi "
           "WHERE status = 1 AND category_id = 1 AND rating >= 4.0 "
           "AND _ST_Dwithin(location, ST_GeomFromText('POINT({CLON} {CLAT})'), {RDEG}) "
           "AND MATCH(name) AGAINST ('+咖啡 +自习' IN BOOLEAN MODE)").replace(
               "{CLON}", str(C_LON)).replace("{CLAT}", str(C_LAT)).replace("{RDEG}", str(R_DEG))

    Q1C = ("SELECT poi_id FROM dim_poi ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10").replace(
        "{QVEC}", qvec)

    Q3A = ("SELECT COUNT(*) FROM dim_poi "
           "WHERE _ST_Dwithin(location, ST_GeomFromText('POINT({CLON} {CLAT})'), {RDEG})").replace(
               "{CLON}", str(C_LON)).replace("{CLAT}", str(C_LAT)).replace("{RDEG}", str(R_DEG))

    Q3B = ("SELECT COUNT(*) FROM dim_poi "
           "WHERE lon BETWEEN {X0} AND {X1} AND lat BETWEEN {Y0} AND {Y1} "
           "AND _ST_Dwithin(location, ST_GeomFromText('POINT({CLON} {CLAT})'), {RDEG})").replace(
               "{X0}", str(X0)).replace("{X1}", str(X1)).replace("{Y0}", str(Y0)).replace(
               "{Y1}", str(Y1)).replace("{CLON}", str(C_LON)).replace("{CLAT}", str(C_LAT)).replace(
               "{RDEG}", str(R_DEG))

    Q4A = ("SELECT poi_id FROM dim_poi "
           "ORDER BY ST_Distance_Sphere(location, ST_GeomFromText('POINT({CLON} {CLAT})')) LIMIT 10").replace(
               "{CLON}", str(C_LON)).replace("{CLAT}", str(C_LAT))

    Q4B = ("SELECT poi_id FROM dim_poi "
           "WHERE lon BETWEEN {X0} AND {X1} AND lat BETWEEN {Y0} AND {Y1} "
           "ORDER BY ST_Distance_Sphere(location, ST_GeomFromText('POINT({CLON} {CLAT})')) LIMIT 10").replace(
               "{X0}", str(X0)).replace("{X1}", str(X1)).replace("{Y0}", str(Y0)).replace(
               "{Y1}", str(Y1)).replace("{CLON}", str(C_LON)).replace("{CLAT}", str(C_LAT))

    Q5A = ("SELECT g.grid_id, COUNT(p.poi_id) FROM fact_traffic_grid g "
           "JOIN dim_poi p ON ST_Contains(g.grid_poly, p.location) "
           "WHERE g.time_bucket = 'workday_evening' AND g.grid_id IN ({GIDS}) "
           "GROUP BY g.grid_id").replace("{GIDS}", GID_LIST)

    Q5B = ("SELECT g.grid_id, COUNT(p.poi_id) FROM fact_traffic_grid g "
           "JOIN dim_poi p ON p.grid_id = g.grid_id "
           "WHERE g.time_bucket = 'workday_evening' AND g.grid_id IN ({GIDS}) "
           "GROUP BY g.grid_id").replace("{GIDS}", GID_LIST)

    Q6 = ("SELECT g.grid_id, SUM(g.flow_cnt), COUNT(p.poi_id) "
          "FROM fact_traffic_grid g LEFT JOIN dim_poi p ON p.grid_id = g.grid_id AND p.category_id = 1 "
          "WHERE g.time_bucket = 'workday_evening' AND g.grid_id IN ({GIDS}) "
          "GROUP BY g.grid_id ORDER BY SUM(g.flow_cnt) DESC LIMIT 10").replace("{GIDS}", GID_LIST)

    T = """
-- =============================================================================
-- GeoMind 生产库验证脚本（由 backend/etl/gen_verify.py 生成，勿手改）
-- 库：geomind   数据：成都主城 20000 POI / 800 铺位 / 2000 网格 ×2 时段 / 60 知识切片
-- 计时：SYSDATE(6) 打点 + 每个查询重复 5 次输出 min/avg
-- =============================================================================
USE geomind;

SELECT '=== 0. 预热（HNSW 冷启动实测可达 670ms，演示前必跑一次） ===' AS sec;
{WARM}

-- -----------------------------------------------------------------------------
-- Q1 四模混合：全文(ngram AND) + 向量(cosine ANN) + 空间(MBR 预筛 + ST 复核) + 标量
-- -----------------------------------------------------------------------------
SELECT '=== Q1 四模混合 top10 ===' AS sec;
SELECT poi_id, name, rating,
       ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT({CLON} {CLAT})')),0) AS dist_m,
       ROUND(cosine_distance(emb,'{QVEC}'),4) AS sem_dist
FROM dim_poi
WHERE status = 1
  AND category_id = 1
  AND rating >= 4.0
  AND lon BETWEEN {X0} AND {X1}
  AND lat BETWEEN {Y0} AND {Y1}
  AND _ST_Dwithin(location, ST_GeomFromText('POINT({CLON} {CLAT})'), {RDEG})
  AND MATCH(name) AGAINST ('+咖啡 +自习' IN BOOLEAN MODE)
ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE
LIMIT 10;

SELECT '=== Q1 计时：四模（含 MBR 预筛） vs 去掉 MBR（其余相同） ===' AS sec;
{T1A}
{T1B}
{T1C}

SELECT '=== Q1 EXPLAIN（含 MBR） ===' AS sec;
EXPLAIN SELECT poi_id, name FROM dim_poi
WHERE status = 1 AND category_id = 1 AND rating >= 4.0
  AND lon BETWEEN {X0} AND {X1} AND lat BETWEEN {Y0} AND {Y1}
  AND _ST_Dwithin(location, ST_GeomFromText('POINT({CLON} {CLAT})'), {RDEG})
  AND MATCH(name) AGAINST ('+咖啡 +自习' IN BOOLEAN MODE)
ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

SELECT '=== Q1 EXPLAIN（去掉 MBR，观察驱动索引变化） ===' AS sec;
EXPLAIN SELECT poi_id, name FROM dim_poi
WHERE status = 1 AND category_id = 1 AND rating >= 4.0
  AND _ST_Dwithin(location, ST_GeomFromText('POINT({CLON} {CLAT})'), {RDEG})
  AND MATCH(name) AGAINST ('+咖啡 +自习' IN BOOLEAN MODE)
ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

-- 对照组：不做任何过滤的纯向量 ANN。若四模结果全部落在「咖啡+自习+1.5km 内」
-- 而本组混杂，即证明多模过滤在索引扫描阶段生效（PRE-FILTER），而非召回后过滤。
SELECT '=== Q1c 对照组：纯向量 ANN top10（无任何过滤） ===' AS sec;
SELECT poi_id, name, category_id,
       ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT({CLON} {CLAT})')),0) AS dist_m,
       ROUND(cosine_distance(emb,'{QVEC}'),4) AS sem_dist
FROM dim_poi
ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

-- -----------------------------------------------------------------------------
-- Q2 五模：四模 + JSON 属性谓词（attrs->'$.parking' / attrs->'$.wifi'）
-- -----------------------------------------------------------------------------
SELECT '=== Q2 五模 top5（含 JSON：可停车 + 有 wifi） ===' AS sec;
SELECT poi_id, name, rating,
       ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT({CLON} {CLAT})')),0) AS dist_m,
       ROUND(cosine_distance(emb,'{QVEC}'),4) AS sem_dist
FROM dim_poi
WHERE status = 1 AND category_id = 1 AND rating >= 4.0
  AND lon BETWEEN {X0} AND {X1} AND lat BETWEEN {Y0} AND {Y1}
  AND MATCH(name) AGAINST ('+咖啡 +自习' IN BOOLEAN MODE)
  AND attrs->'$.parking' = true
  AND attrs->'$.wifi' = true
ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 5;

-- -----------------------------------------------------------------------------
-- Q3 空间圈选：纯 _ST_Dwithin 基线 vs MBR 预筛 + ST 复核（同一结果集）
-- -----------------------------------------------------------------------------
SELECT '=== Q3a 圈选基线（纯 _ST_Dwithin，全扫） ===' AS sec;
{T3A}
SELECT '=== Q3b 圈选 MBR 预筛 + ST 复核 ===' AS sec;
{T3B}

SELECT '=== Q3b EXPLAIN（看是否 RANGE SCAN idx_poi_bbox） ===' AS sec;
EXPLAIN SELECT COUNT(*) FROM dim_poi
WHERE lon BETWEEN {X0} AND {X1} AND lat BETWEEN {Y0} AND {Y1}
  AND _ST_Dwithin(location, ST_GeomFromText('POINT({CLON} {CLAT})'), {RDEG});

-- -----------------------------------------------------------------------------
-- Q4 KNN：基线（全表排序） vs MBR 预筛（真实球面距离，米）
-- -----------------------------------------------------------------------------
SELECT '=== Q4 KNN 结果（应当与基线完全一致） ===' AS sec;
SELECT poi_id, ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT({CLON} {CLAT})')),0) AS dist_m
FROM dim_poi
WHERE lon BETWEEN {X0} AND {X1} AND lat BETWEEN {Y0} AND {Y1}
ORDER BY ST_Distance_Sphere(location, ST_GeomFromText('POINT({CLON} {CLAT})')) LIMIT 10;
SELECT '=== Q4 计时 ===' AS sec;
{T4A}
{T4B}

-- -----------------------------------------------------------------------------
-- Q5 空间连接：ST_Contains JOIN vs grid_id 冗余列 JOIN（春熙路周边 25 网格）
-- -----------------------------------------------------------------------------
SELECT '=== Q5a 空间连接基线（ST_Contains，全扫）结果 ===' AS sec;
SELECT g.grid_id, COUNT(p.poi_id) AS poi_cnt
FROM fact_traffic_grid g JOIN dim_poi p ON ST_Contains(g.grid_poly, p.location)
WHERE g.time_bucket = 'workday_evening' AND g.grid_id IN ({GIDS})
GROUP BY g.grid_id ORDER BY g.grid_id LIMIT 10;
SELECT '=== Q5 计时 ===' AS sec;
{T5A}
{T5B}

SELECT '=== Q5b EXPLAIN（冗余列 JOIN） ===' AS sec;
EXPLAIN SELECT g.grid_id, COUNT(p.poi_id) FROM fact_traffic_grid g JOIN dim_poi p ON p.grid_id = g.grid_id
WHERE g.time_bucket = 'workday_evening' AND g.grid_id IN ({GIDS}) GROUP BY g.grid_id;

-- -----------------------------------------------------------------------------
-- Q6 网格客流聚合（选址场景真实形态：客流 × POI 密度 × 评分）
-- -----------------------------------------------------------------------------
SELECT '=== Q6 网格聚合结果（workday_evening，按客流降序） ===' AS sec;
SELECT g.grid_id, SUM(g.flow_cnt) AS flow, COUNT(p.poi_id) AS poi_cnt,
       ROUND(AVG(p.rating),2) AS avg_rating
FROM fact_traffic_grid g LEFT JOIN dim_poi p ON p.grid_id = g.grid_id AND p.category_id = 1
WHERE g.time_bucket = 'workday_evening' AND g.grid_id IN ({GIDS})
GROUP BY g.grid_id ORDER BY flow DESC LIMIT 10;
SELECT '=== Q6 计时 ===' AS sec;
{T6}

-- -----------------------------------------------------------------------------
-- Q7 知识库混合检索（全文 + 向量）
-- -----------------------------------------------------------------------------
SELECT '=== Q7 知识库：全文 + 向量 top5 ===' AS sec;
SELECT doc_id, title, ROUND(cosine_distance(emb,'{QVEC_KB}'),4) AS sem_dist
FROM knowledge_doc
WHERE MATCH(text) AGAINST ('+咖啡 +选址' IN BOOLEAN MODE)
ORDER BY cosine_distance(emb,'{QVEC_KB}') APPROXIMATE LIMIT 5;
SELECT '=== Q7 EXPLAIN ===' AS sec;
EXPLAIN SELECT doc_id FROM knowledge_doc
WHERE MATCH(text) AGAINST ('+咖啡 +选址' IN BOOLEAN MODE)
ORDER BY cosine_distance(emb,'{QVEC_KB}') APPROXIMATE LIMIT 5;
SELECT '=== Q7 纯向量对照（无全文过滤）top5 ===' AS sec;
SELECT doc_id, title, ROUND(cosine_distance(emb,'{QVEC_KB}'),4) AS sem_dist
FROM knowledge_doc
ORDER BY cosine_distance(emb,'{QVEC_KB}') APPROXIMATE LIMIT 5;

-- -----------------------------------------------------------------------------
-- Q8 Spatial State 读写与增量更新
-- -----------------------------------------------------------------------------
SELECT '=== Q8a 读取 Spatial State ===' AS sec;
SELECT session_id, scene, version,
       JSON_EXTRACT(state,'$.slots.intent') AS intent,
       JSON_EXTRACT(state,'$.slots.radius_m') AS radius_m,
       JSON_LENGTH(JSON_EXTRACT(state,'$.steps')) AS step_cnt
FROM scene_solution WHERE session_id = 'demo-site-selection-001';

SELECT '=== Q8b 增量更新（改半径 + 推进步骤状态） ===' AS sec;
UPDATE scene_solution
   SET version = version + 1,
       state = JSON_SET(state, '$.slots.radius_m', 2000,
                               '$.steps[1].status', 'done',
                               '$.steps[2].status', 'ready')
 WHERE session_id = 'demo-site-selection-001';
SELECT session_id, version,
       JSON_EXTRACT(state,'$.slots.radius_m') AS radius_m,
       JSON_EXTRACT(state,'$.steps[1].status') AS s2,
       JSON_EXTRACT(state,'$.steps[2].status') AS s3
FROM scene_solution WHERE session_id = 'demo-site-selection-001';
-- 还原，保证脚本可重复执行
UPDATE scene_solution
   SET version = 1,
       state = JSON_SET(state, '$.slots.radius_m', 1500,
                               '$.steps[1].status', 'ready',
                               '$.steps[2].status', 'pending')
 WHERE session_id = 'demo-site-selection-001';

-- -----------------------------------------------------------------------------
-- Q9 冗余列一致性与数据体检（三项必须全 0）
-- -----------------------------------------------------------------------------
SELECT '=== Q9 冗余列一致性（null/mismatch 必须全 0） ===' AS sec;
SELECT SUM(CASE WHEN lon IS NULL OR lat IS NULL OR grid_id IS NULL THEN 1 ELSE 0 END) AS null_redundant,
       SUM(CASE WHEN ABS(lon-ST_X(location))>1e-9 OR ABS(lat-ST_Y(location))>1e-9 THEN 1 ELSE 0 END) AS xy_mismatch,
       SUM(CASE WHEN grid_id <> CAST(FLOOR((ST_Y(location)-30.45)/0.01) AS SIGNED)*50
                              + CAST(FLOOR((ST_X(location)-103.85)/0.01) AS SIGNED)+1 THEN 1 ELSE 0 END) AS gid_mismatch,
       COUNT(*) AS total
FROM dim_poi;

SELECT '=== Q9b 类目分布 ===' AS sec;
SELECT c.name_l2, COUNT(*) AS cnt
FROM dim_poi p JOIN dim_category c ON c.category_id = p.category_id
GROUP BY c.name_l2 ORDER BY cnt DESC;
"""

    sql = (T.replace("{QVEC}", qvec)
            .replace("{QVEC_KB}", qvec_kb)
            .replace("{CLON}", str(C_LON))
            .replace("{CLAT}", str(C_LAT))
            .replace("{RDEG}", str(R_DEG))
            .replace("{X0}", str(X0)).replace("{X1}", str(X1))
            .replace("{Y0}", str(Y0)).replace("{Y1}", str(Y1))
            .replace("{GIDS}", GID_LIST)
            .replace("{WARM}", timed("warmup_ann", Q1C, n=1))
            .replace("{T1A}", timed("Q1a_four_mode_with_mbr", Q1A))
            .replace("{T1B}", timed("Q1b_four_mode_no_mbr", Q1B))
            .replace("{T1C}", timed("Q1c_pure_vector_ann", Q1C))
            .replace("{T3A}", timed("Q3a_baseline_dwithin", Q3A))
            .replace("{T3B}", timed("Q3b_mbr_prefilter", Q3B))
            .replace("{T4A}", timed("Q4a_knn_baseline", Q4A))
            .replace("{T4B}", timed("Q4b_knn_mbr", Q4B))
            .replace("{T5A}", timed("Q5a_spatial_join", Q5A))
            .replace("{T5B}", timed("Q5b_gridid_join", Q5B))
            .replace("{T6}", timed("Q6_grid_agg", Q6)))

    out = os.path.join(DATA, "verify_prod.sql")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(sql)
    print("[verify] 生成 %s  (%.2f MB)" % (out, os.path.getsize(out) / 1e6))


if __name__ == "__main__":
    main()
