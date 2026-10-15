-- GeoMind 10 万级压测 · 耗时测量（第三部分）
-- 说明：mysql 批处理模式（-e / source）不打印 "x rows in set (n.nnn sec)"，
--      故采用服务端 NOW(6) 在每条查询前后打点，差值 = 执行 + 结果回传耗时。
-- 由 build_bench_sql.py 注入 {QVEC} 后执行。

USE geomind_bench;

SELECT 'T0_warm' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS warm FROM t_bench_poi;
SELECT 'T0_warm' AS q, 'end' AS p, NOW(6) AS t;

-- ---------- 空间圈选 ----------
SELECT 'T1_dwithin_220m' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS c FROM t_bench_poi WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.002);
SELECT 'T1_dwithin_220m' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T2_dwithin_1km' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS c FROM t_bench_poi WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.01);
SELECT 'T2_dwithin_1km' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T3_dwithin_5km' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS c FROM t_bench_poi WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.05);
SELECT 'T3_dwithin_5km' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T4_contains_box' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS c FROM t_bench_poi WHERE ST_Contains(ST_GeomFromText('POLYGON((104.00 30.60,104.15 30.60,104.15 30.72,104.00 30.72,104.00 30.60))'), location);
SELECT 'T4_contains_box' AS q, 'end' AS p, NOW(6) AS t;

-- ---------- KNN ----------
SELECT 'T5_knn_fullsort' AS q, 'start' AS p, NOW(6) AS t;
SELECT id, ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')),0) AS dist_m FROM t_bench_poi ORDER BY ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')) LIMIT 10;
SELECT 'T5_knn_fullsort' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T6_knn_prefilter' AS q, 'start' AS p, NOW(6) AS t;
SELECT id, ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')),0) AS dist_m FROM t_bench_poi WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.01) ORDER BY ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')) LIMIT 10;
SELECT 'T6_knn_prefilter' AS q, 'end' AS p, NOW(6) AS t;

-- ---------- 标量 / 全文 / JSON / 聚合 ----------
SELECT 'T7_scalar_idx' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS c FROM t_bench_poi WHERE category=0 AND status=1;
SELECT 'T7_scalar_idx' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T8_fulltext' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS c FROM t_bench_poi WHERE MATCH(name) AGAINST('咖啡');
SELECT 'T8_fulltext' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T9_json' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS c FROM t_bench_poi WHERE JSON_EXTRACT(props,'$.price') >= 200;
SELECT 'T9_json' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T10_groupby_all' AS q, 'start' AS p, NOW(6) AS t;
SELECT category, COUNT(*) AS cnt FROM t_bench_poi GROUP BY category;
SELECT 'T10_groupby_all' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T11_spatial_agg' AS q, 'start' AS p, NOW(6) AS t;
SELECT category, COUNT(*) AS cnt FROM t_bench_poi WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.02) GROUP BY category;
SELECT 'T11_spatial_agg' AS q, 'end' AS p, NOW(6) AS t;

-- ---------- 空间连接（最慢，单独计时） ----------
SELECT 'T12_spatial_join' AS q, 'start' AS p, NOW(6) AS t;
SELECT g.grid_id, COUNT(p.id) AS cnt FROM t_bench_grid g JOIN t_bench_poi p ON ST_Contains(g.cell, p.location) GROUP BY g.grid_id ORDER BY g.grid_id LIMIT 5;
SELECT 'T12_spatial_join' AS q, 'end' AS p, NOW(6) AS t;

-- ---------- 向量 ANN ----------
SELECT 'T13_ann_approx' AS q, 'start' AS p, NOW(6) AS t;
SELECT id, ROUND(cosine_distance(emb,'{QVEC}'),4) AS d FROM t_bench_poi WHERE emb IS NOT NULL ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;
SELECT 'T13_ann_approx' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T14_ann_exact' AS q, 'start' AS p, NOW(6) AS t;
SELECT id, ROUND(cosine_distance(emb,'{QVEC}'),4) AS d FROM t_bench_poi WHERE emb IS NOT NULL ORDER BY cosine_distance(emb,'{QVEC}') LIMIT 10;
SELECT 'T14_ann_exact' AS q, 'end' AS p, NOW(6) AS t;

-- ---------- 三模混合 ----------
SELECT 'T15_hybrid' AS q, 'start' AS p, NOW(6) AS t;
SELECT id, name, rating, ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')),0) AS dist_m, ROUND(cosine_distance(emb,'{QVEC}'),4) AS sem FROM t_bench_poi WHERE category=0 AND status=1 AND rating>=4.0 AND _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.02) ORDER BY cosine_distance(emb,'{QVEC}') APPROXIMATE LIMIT 10;
SELECT 'T15_hybrid' AS q, 'end' AS p, NOW(6) AS t;

SELECT 'T16_hybrid_count' AS q, 'start' AS p, NOW(6) AS t;
SELECT COUNT(*) AS c FROM t_bench_poi WHERE status=1 AND rating>=4.0 AND _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.02);
SELECT 'T16_hybrid_count' AS q, 'end' AS p, NOW(6) AS t;
