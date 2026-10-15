USE geomind;
SELECT '===== 1. 空间：距离（米，以A点为锚）=====' AS sec;
SELECT name, ROUND(ST_Distance_Sphere(location, (SELECT location FROM t_geo WHERE name='A')),1) AS dist_m
FROM t_geo ORDER BY dist_m;

SELECT '===== 2. 空间：_ST_Dwithin（单位度，0.05度）=====' AS sec;
SELECT name FROM t_geo
WHERE _ST_Dwithin(location, (SELECT location FROM t_geo WHERE name='A'), 0.05);

SELECT '===== 3. 空间：ST_Intersects + ST_Buffer =====' AS sec;
SELECT name FROM t_geo
WHERE ST_Intersects(location, ST_Buffer((SELECT location FROM t_geo WHERE name='A'), 0.05));

SELECT '===== 4. 向量：ANN（cosine + APPROXIMATE）=====' AS sec;
SELECT id, content, ROUND(cosine_distance(emb, '[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]'),4) AS d
FROM t_vec ORDER BY cosine_distance(emb, '[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]') APPROXIMATE LIMIT 3;

SELECT '===== 5. 全文：ngram MATCH AGAINST（咖啡）=====' AS sec;
SELECT id, title, ROUND(MATCH(title) AGAINST('咖啡'),3) AS score
FROM t_ft WHERE MATCH(title) AGAINST('咖啡');

SELECT '===== 6. JSON：-> 提取 =====' AS sec;
SELECT id, JSON_EXTRACT(attrs,'$.rent') AS rent, attrs->'$.metro' AS metro FROM t_json;

SELECT '===== 7. 三模混合：空间圈+标量(status=1,cat=1)+向量ANN =====' AS sec;
SELECT id, name, rating,
  ROUND(ST_Distance_Sphere(location,(SELECT location FROM t_hybrid WHERE id=1)),0) AS dist_m,
  ROUND(cosine_distance(emb,'[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]'),4) AS vd
FROM t_hybrid
WHERE status=1 AND cat=1
  AND _ST_Dwithin(location,(SELECT location FROM t_hybrid WHERE id=1),0.05)
ORDER BY cosine_distance(emb,'[0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8]') APPROXIMATE LIMIT 5;
