-- GeoMind 10 万级压测矩阵（SeekDB V1.4 / OceanBase 4.3.5.3）
-- 压测对象：t_bench_poi 10 万行（含 2 万行 1024 维向量）、t_bench_grid 100 网格
-- 用法：由 build_bench_sql.py 注入查询向量 {QVEC} 后执行；耗时读数取 mysql 客户端输出的
--      "x rows in set (n.nnn sec)"
-- 中心点：成都天府广场附近 (104.0668, 30.6598)；1 度纬度 ≈ 111 km，0.001 度 ≈ 111 m

USE geomind_bench;

-- ============================ 预热 ============================
SELECT '=== W. 预热（触发 plan cache） ===' AS sec;
SELECT COUNT(*) AS total FROM t_bench_poi;

-- ================= A. 空间圈选：SPATIAL INDEX 是否命中 =================
SELECT '=== A1. EXPLAIN 圈选 0.002 度 (~220m) ===' AS sec;
EXPLAIN SELECT COUNT(*) FROM t_bench_poi
 WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.002);
SELECT '=== A1. 实跑 0.002 度 ===' AS sec;
SELECT COUNT(*) AS cnt_220m FROM t_bench_poi
 WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.002);

SELECT '=== A2. 实跑 0.01 度 (~1.1km) ===' AS sec;
SELECT COUNT(*) AS cnt_1km FROM t_bench_poi
 WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.01);

SELECT '=== A3. 实跑 0.05 度 (~5.5km) ===' AS sec;
SELECT COUNT(*) AS cnt_5km FROM t_bench_poi
 WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.05);

SELECT '=== A4. 对照：IGNORE INDEX(idx_loc) 强制不用空间索引，0.01 度 ===' AS sec;
SELECT COUNT(*) AS cnt_1km_noindex FROM t_bench_poi IGNORE INDEX(idx_loc)
 WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.01);

-- ================= B. KNN 最近邻：全表排序 vs 圈选预筛 =================
SELECT '=== B1. KNN 全表距离排序 LIMIT 10（无空间过滤） ===' AS sec;
SELECT id, ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')),0) AS dist_m
 FROM t_bench_poi
 ORDER BY ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')) LIMIT 10;

SELECT '=== B2. 圈选预筛(0.01度) + 距离排序 LIMIT 10 ===' AS sec;
SELECT id, ROUND(ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')),0) AS dist_m
 FROM t_bench_poi
 WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.01)
 ORDER BY ST_Distance_Sphere(location, ST_GeomFromText('POINT(104.0668 30.6598)')) LIMIT 10;

-- ================= C. 标量 / 全文 / JSON =================
SELECT '=== C1. EXPLAIN 标量过滤 category+status ===' AS sec;
EXPLAIN SELECT COUNT(*) FROM t_bench_poi WHERE category=0 AND status=1;
SELECT '=== C1. 实跑标量过滤 ===' AS sec;
SELECT COUNT(*) AS cat0_open FROM t_bench_poi WHERE category=0 AND status=1;

SELECT '=== C2. 全文 ngram 检索「咖啡」 ===' AS sec;
SELECT COUNT(*) AS ft_cnt FROM t_bench_poi WHERE MATCH(name) AGAINST('咖啡');
SELECT '=== C2. EXPLAIN 全文 ===' AS sec;
EXPLAIN SELECT id, name FROM t_bench_poi WHERE MATCH(name) AGAINST('咖啡') LIMIT 10;

SELECT '=== C3. JSON 属性过滤 props->price ===' AS sec;
SELECT COUNT(*) AS json_cnt FROM t_bench_poi WHERE JSON_EXTRACT(props,'$.price') >= 200;

SELECT '=== C4. 全表分组聚合 GROUP BY category ===' AS sec;
SELECT category, COUNT(*) AS cnt, ROUND(AVG(rating),2) AS avg_rating
 FROM t_bench_poi GROUP BY category ORDER BY category;

SELECT '=== C5. 空间圈选 + 分组聚合（下推聚合） ===' AS sec;
SELECT category, COUNT(*) AS cnt FROM t_bench_poi
 WHERE _ST_Dwithin(location, ST_GeomFromText('POINT(104.0668 30.6598)'), 0.02)
 GROUP BY category ORDER BY category;
