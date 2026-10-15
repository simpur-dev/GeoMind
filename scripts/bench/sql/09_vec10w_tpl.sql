-- GeoMind 10 万级向量压测（补齐 emb 到 10 万行之后）
-- 关注：HNSW 是否命中、ANN 与精确的耗时差、召回是否一致
-- 由 build_bench_sql.py 注入 {QVEC} 后执行

USE geomind_bench;

SELECT 'V0_warm' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS total, COUNT(emb) AS with_vec FROM t_bench_poi;
SELECT 'V0_warm' AS q, 'end' AS p, NOW(6) AS t;

SELECT '=== V1. EXPLAIN ANN（10 万向量） ===' AS s;
EXPLAIN SELECT id, cosine_distance(emb,'{QVEC}') AS d FROM t_bench_poi ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

SELECT 'V2_ann_approx' AS q, 'start' AS p, NOW(6) AS t;
SELECT id, ROUND(cosine_distance(emb,'{QVEC}'),4) AS d FROM t_bench_poi ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;
SELECT 'V2_ann_approx' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'V3_ann_exact' AS q, 'start' AS p, NOW(6) AS t;
SELECT id, ROUND(cosine_distance(emb,'{QVEC}'),4) AS d FROM t_bench_poi ORDER BY cosine_distance(emb,'{QVEC}') LIMIT 10;
SELECT 'V3_ann_exact' AS q, 'end' AS p, NOW(6) AS t;

SELECT '=== V4. 标量过滤 + ANN（走 idx_cat 还是 HNSW） ===' AS s;
EXPLAIN SELECT id, ROUND(cosine_distance(emb,'{QVEC}'),4) AS d FROM t_bench_poi WHERE category=0 ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

SELECT 'V5_filtered_ann' AS q, 'start' AS p, NOW(6) AS t;
SELECT id, ROUND(cosine_distance(emb,'{QVEC}'),4) AS d FROM t_bench_poi WHERE category=0 ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;
SELECT 'V5_filtered_ann' AS q, 'end' AS p, NOW(6) AS t;

SELECT '=== V6. 四模混合：MBR 预筛 + 标量 + 空间 + 语义 ===' AS s;
EXPLAIN SELECT id, name, ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')),0) AS dist_m,
   ROUND(cosine_distance(emb,'{QVEC}'),4) AS sem
 FROM t_bench_poi
 WHERE lon BETWEEN 104.0468 AND 104.0868 AND lat BETWEEN 30.6398 AND 30.6798
   AND category=0 AND status=1 AND rating>=4.0
   AND _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.02)
 ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;

SELECT 'V6_hybrid_mbr' AS q, 'start' AS p, NOW(6) AS t;
SELECT id, name, ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')),0) AS dist_m,
   ROUND(cosine_distance(emb,'{QVEC}'),4) AS sem
 FROM t_bench_poi
 WHERE lon BETWEEN 104.0468 AND 104.0868 AND lat BETWEEN 30.6398 AND 30.6798
   AND category=0 AND status=1 AND rating>=4.0
   AND _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.02)
 ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;
SELECT 'V6_hybrid_mbr' AS q, 'end' AS p, NOW(6) AS t;

SELECT '=== V7. 存储体积（10 万向量后） ===' AS s;
SELECT table_name, table_rows, ROUND(data_length/1024/1024,1) AS data_mb, ROUND(index_length/1024/1024,1) AS index_mb
 FROM information_schema.tables WHERE table_schema='geomind_bench';
