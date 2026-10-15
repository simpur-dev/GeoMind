-- GeoMind 10 万级压测矩阵 · 第二部分（耗时较长的空间连接 / 向量 / 混合检索）
-- 由 build_bench_sql.py 注入查询向量 {QVEC} 后执行

USE geomind_bench;

-- ================= D. 空间连接：100 网格 × 10 万点 =================
SELECT '=== D1. EXPLAIN 空间连接 ST_Contains + GROUP BY ===' AS sec;
EXPLAIN SELECT g.grid_id, COUNT(p.id) AS cnt
 FROM t_bench_grid g JOIN t_bench_poi p ON ST_Contains(g.cell, p.location)
 GROUP BY g.grid_id;

SELECT '=== D2. 实跑 空间连接（全量 100 网格 × 10 万点） ===' AS sec;
SELECT g.grid_id, COUNT(p.id) AS cnt, ROUND(AVG(p.rating),2) AS avg_rating
 FROM t_bench_grid g JOIN t_bench_poi p ON ST_Contains(g.cell, p.location)
 GROUP BY g.grid_id ORDER BY g.grid_id LIMIT 20;

SELECT '=== D3. 单网格归属统计（网格 55） ===' AS sec;
SELECT COUNT(*) AS in_grid55 FROM t_bench_poi p, t_bench_grid g
 WHERE g.grid_id=55 AND ST_Contains(g.cell, p.location);

SELECT '=== D4. 备选方案：_ST_Dwithin 预筛（网格中心 ±0.025 度） ===' AS sec;
SELECT COUNT(*) AS near_center_cnt FROM t_bench_poi
 WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.1000 30.6500)'), 0.025);

-- ================= E. 向量 ANN：HNSW 是否命中 =================
SELECT '=== E1. EXPLAIN 近似 ANN（APPROXIMATE, 仅向量排序） ===' AS sec;
EXPLAIN SELECT id, cosine_distance(emb,'{QVEC}') AS d
 FROM t_bench_poi ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

SELECT '=== E2. 实跑 近似 ANN top10 ===' AS sec;
SELECT id, ROUND(cosine_distance(emb,'{QVEC}'),4) AS d
 FROM t_bench_poi ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

SELECT '=== E3. 实跑 精确 top10（无 APPROXIMATE，全表算） ===' AS sec;
SELECT id, ROUND(cosine_distance(emb,'{QVEC}'),4) AS d
 FROM t_bench_poi WHERE emb IS NOT NULL
 ORDER BY cosine_distance(emb,'{QVEC}') LIMIT 10;

SELECT '=== E4. 实跑 近似 ANN + 向量非空过滤（看过滤是否影响走索引） ===' AS sec;
SELECT id, ROUND(cosine_distance(emb,'{QVEC}'),4) AS d
 FROM t_bench_poi WHERE emb IS NOT NULL
 ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

-- ================= F. 三模混合：空间 + 标量 + 语义（PRE-FILTER） =================
SELECT '=== F1. EXPLAIN 三模混合 PRE-FILTER ===' AS sec;
EXPLAIN SELECT id, name, rating,
   ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')),0) AS dist_m,
   ROUND(cosine_distance(emb,'{QVEC}'),4) AS sem
 FROM t_bench_poi
 WHERE category=0 AND status=1 AND rating>=4.0
   AND _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.02)
 ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

SELECT '=== F2. 实跑 三模混合 ===' AS sec;
SELECT id, name, rating,
   ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')),0) AS dist_m,
   ROUND(cosine_distance(emb,'{QVEC}'),4) AS sem
 FROM t_bench_poi
 WHERE category=0 AND status=1 AND rating>=4.0
   AND _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.02)
 ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

SELECT '=== F3. 实跑 三模混合（标量放宽：全类别） ===' AS sec;
SELECT COUNT(*) AS hybrid_cnt FROM t_bench_poi
 WHERE status=1 AND rating>=4.0
   AND _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.02);

-- ================= G. 存储与索引体积 =================
SELECT '=== G1. 表/索引体积 ===' AS sec;
SELECT table_name, table_rows,
   ROUND(data_length/1024/1024,1) AS data_mb,
   ROUND(index_length/1024/1024,1) AS index_mb
 FROM information_schema.tables WHERE table_schema='geomind_bench';
