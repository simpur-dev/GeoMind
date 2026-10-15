# OceanBase 空间与向量能力调研

> 调研时间：2026-10-15。调研方式：逐条打开 OceanBase 官方文档站（www.oceanbase.com / en.oceanbase.com / open.oceanbase.com）核实。
> 范围：OceanBase 4.x（重点 4.3.x / 4.4.x），**MySQL 模式**（赛题使用模式）；Oracle 模式仅作对照。
> 标注约定：【已查证】= 官方文档原文核实；【未核实】= 未在官方文档找到明确说法，需赛题环境实测。

---

## 1. 版本与文档依据（实际打开的官方 URL）

| 文档 | 版本 | URL |
|---|---|---|
| 空间关系函数 | V4.3.0（MySQL 模式） | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000644024 |
| 空间运算函数 | V4.3.0（MySQL 模式） | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000644025 |
| 几何属性函数 | V4.2.2 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000511875 |
| 空间运算函数（含 _ST_* 私有函数） | V4.2.2 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000511877 |
| 空间数据格式（WKT/WKB 合法性） | V4.2.2 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000512167 |
| 几何构造函数（ST_GeomFromText 等，英文版） | V4.2.1 | https://en.oceanbase.com/docs/common-oceanbase-database-10000000001106883 |
| 创建空间列 | V4.3.3 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000001433667 |
| 空间数据类型概述 | V4.3.5 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000002018068 |
| 创建索引（含空间索引 SPATIAL INDEX） | V4.3.1 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000820750 |
| What's New（MySQL GIS 增强 / SDO_GEOMETRY） | V4.3.1 | https://en.oceanbase.com/docs/common-oceanbase-database-10000000001375280 |
| What's New（向量类型及索引，实验特性） | V4.3.3 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000001509391 |
| 创建向量索引 | V4.3.3 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000001484244 |
| 创建向量索引（含 cosine/HNSW_SQ/IVF/过滤条件说明） | V4.4.0 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003378338 |
| 向量搜索快速上手（SQL 写法） | V4.4.1 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003976681 |
| 向量搜索概述（混合搜索/分布式/事务） | V4.4.1 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003976682 |
| SELECT 语法（向量距离函数名） | V4.4.1 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003981642 |
| HNSW 系列索引参数 | V4.5.0 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000004827714 |
| **基于 OceanBase 构建文旅小助手（交互式教程）** | V1.0.0（2026-08-17） | https://www.oceanbase.com/docs/common-interactive-tutorials-1000000002963909 |
| 基于 OceanBase 多模融合构建文旅小助手（文档版） | V4.5.0 | https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000004475454 |
| 文旅小助手（OB Cloud 控制台版） | — | https://www.oceanbase.com/docs/enterprise-oceanbase-cloud-1000000002963136 |
| demo 仓库 | — | https://github.com/oceanbase-labs/TravelAssist（原 oceanbase-devhub/ob-multi-model-search-demo） |

---

## 2. 空间数据类型与函数

### 2.1 空间数据类型（MySQL 模式）【已查证】

V4.3.3「创建空间列」原文示例：

```sql
CREATE TABLE spatial_data (
  id INT AUTO_INCREMENT PRIMARY KEY,
  geom_point POINT,                          -- 点
  geom_linestring LINESTRING NOT NULL SRID 4326,  -- 线
  geom_polygon POLYGON,                      -- 多边形
  geom_multipoint MULTIPOINT,                -- 多点
  geom_multilinestring MULTILINESTRING,      -- 多线
  geom_multipolygon MULTIPOLYGON,            -- 多多边形
  geom_geometrycollection GEOMETRYCOLLECTION,-- 几何集合
  geom_geometry GEOMETRY                      -- 通用几何，可存任意几何
);
```

- 支持类型：`GEOMETRY / POINT / LINESTRING / POLYGON / MULTIPOINT / MULTILINESTRING / MULTIPOLYGON / GEOMETRYCOLLECTION`（8 种，MySQL GIS 标准集合）。
- **SRID 列级定义**：列上可显式声明 `SRID 4326`；定义 SRID 后，插入其他 SRID 的几何会报错；未定义 SRID 的列优化器不会选择空间索引（但仍会生成索引记录）。
- **使用前需导入 SRS 元数据**【已查证】：部署后需执行 `import_srs_data.py` 导入 `default_srs_data_mysql.sql`（官方称导入后 `__all_spatial_reference_systems` 约 5152 条 SRS 记录）。赛题环境若已内置可跳过。
- Oracle 模式对照：V4.3.1 起支持 `SDO_GEOMETRY` 类型及成员函数 `GET_DIMS()/GET_GTYPE()/ST_ISVALID()`，转换函数 `GET_WKB()/GET_WKT()/GET_GEOJSON()`。**赛题是 MySQL 模式，SDO_GEOMETRY 用不上。**

### 2.2 WKT / WKB 输入输出【已查证】

```sql
ST_GeomFromText(wkt [, srid [, options]])   -- 通用 WKT→几何
ST_GeometryFromText(wkt [, srid [, options]])  -- 同义别名
ST_GeomFromWKB(wkb [, srid [, options]])    -- WKB(BLOB)→几何
ST_GeometryFromWKB(wkb [, srid [, options]])
-- options: 'axis-order=long-lat' | 'lat-long' | 'srid-defined'（默认），可纠正经纬度轴序
ST_AsText(geom)    -- 几何→WKT（全文档大量示例使用）
```
- 官方文档未在本次抓取中单列「输出函数」专页，`ST_AsText` 在所有官方示例中被使用；`ST_AsBinary/ST_AsWKB`【未核实】（按 MySQL 兼容性推断应存在，未逐字核实）。
- V4.3.1 起新增 `ST_AsGeoJSON`【已查证】。

### 2.3 空间函数语法速查表（MySQL 模式）

#### A. 关系判断【已查证，V4.3.0 + V4.3.1 增补】

| 函数 | 语法 | 说明 |
|---|---|---|
| ST_Intersects | `ST_Intersects(g1, g2)` | 是否相交，返回 1/0 |
| ST_Contains | `ST_Contains(g1, g2)` | g1 是否完全包含 g2 |
| ST_Within | `ST_Within(g1, g2)` | g1 是否在 g2 内（与 Contains 互反） |
| ST_Distance | `ST_Distance(g1, g2 [, unit])` | SRS 长度单位；unit 可取 'metre'/'foot' 等 |
| ST_Distance_Sphere | `ST_Distance_Sphere(g1, g2 [, radius])` | **球面最短距离，单位米**；仅支持 Point/MultiPoint |
| ST_IsValid | `ST_IsValid(g)` | 几何合法性检查 |
| ST_Crosses | V4.3.1 新增 | 线面/线线交叉（MySQL 兼容表达式） |
| ST_Overlaps | V4.3.1 新增 | 重叠 |
| _ST_Dwithin | `_ST_Dwithin(g1, g2, distance)` | **OB 私有：两几何是否在指定距离内（距离单位随坐标系）** |
| _ST_Covers | `_ST_Covers(g1, g2)` | OB 私有：g2 是否被 g1 覆盖 |
| _ST_Touches / _ST_Equals | V4.3.1 新增 | PG 兼容私有函数 |

#### B. 几何属性【已查证，V4.2.2】

| 函数 | 语法 | 说明 |
|---|---|---|
| ST_Area | `ST_Area({poly|mpoly})` | 仅 Polygon/MultiPolygon 面积 |
| ST_SRID | `ST_SRID(g [, srid])` | 读/设 SRID（不改坐标） |
| ST_X / ST_Y | `ST_X(p [, new_x])` | Point 的 X/Y 坐标 |
| ST_Longitude / ST_Latitude | `ST_Longitude(p [, val])` | 地理 SRS Point 的经/纬度 |
| _ST_SetSRID | `_ST_SetSRID(geom, srid)` | OB 私有 |

#### C. 几何运算（生成新几何）【已查证】

| 函数 | 语法 | 说明 |
|---|---|---|
| ST_Buffer | `ST_Buffer(g, d [, s1, s2, s3])` | 缓冲区；d ≥ 0.1 |
| ST_Buffer_Strategy | `ST_Buffer_Strategy('point_circle'|'point_square'|'join_round'|'join_miter'|'end_round'|'end_flat' [, n])` | 缓冲区策略 |
| ST_Transform | `ST_Transform(g, target_srid)` | SRS 投影转换（仅地理 SRS） |
| ST_Difference | `ST_Difference(g1, g2)` | 差集 A−B |
| ST_Union | `ST_Union(g1, g2)` | 两几何并集 |
| ST_SymDifference | `ST_SymDifference(g1, g2)` | 对称差 |
| ST_Length | V4.3.1 新增 | 线长度（MySQL 兼容表达式） |
| ST_Centroid | V4.3.1 新增 | 多边形质心 |
| _ST_Buffer / _ST_Transform | OB 私有 | 支持 quad_segs/endcap/join/side 等 PostGIS 风格参数 |
| _ST_ClipByBox2D | `_ST_ClipByBox2D(geom, box2d)` | 与包围盒剪裁 |
| _ST_MakeEnvelope | `_ST_MakeEnvelope(x1,y1,x2,y2)` | 生成矩形 envelope（V4.3.1） |
| ST_AsMVTGeom / _ST_AsMVT | V4.3.1 新增 | MVT 瓦片输出（PG 兼容） |

#### D. 明确"不存在 / 未核实"的函数

- **ST_Union_Agg（空间聚合函数）：未核实，官方文档中未见。** V4.3.1 的 What's New 与函数专页均只列出二元 `ST_Union(g1,g2)`，没有聚合版 union。GeoMind 若需要"把一组多边形合并成一个"，只能在应用层循环调用 `ST_Union` 或用 `_ST_ClipByBox2D` 替代思路。
- **ST_Intersection(g1,g2) 独立函数：未核实。** 官方仅在 `ST_SymDifference` 的说明文字里提到 `ST_Intersection(A,B)` 作为概念；函数专页未单列。需赛题环境 `SELECT ST_Intersection(...)` 实测。
- **MBR 前缀函数族（MBRContains/MBRWithin/MBRIntersects 等 MySQL 原生 MBR 函数）：未核实。** OB 空间索引内部基于 MBR（见第 3 节），但函数专页未列 MBR* 函数；按 MySQL 兼容性推断大概率存在，未逐字核实。
- **SDO_Within_Distance（Oracle 风格）：MySQL 模式不适用**，对应物是 OB 私有的 `_ST_Dwithin`。
- **空间连接（spatial join）**：无专门算子/语法，文档未提及"空间连接"优化特性【未核实】。常规做法是 `JOIN ... ST_Contains(...) / _ST_Dwithin(...)` 作为连接条件，OB 的 Nested Loop Join 可用于任意连接条件【已查证联接算法文档】，但是否有空间感知的 join 优化（如索引嵌套循环 + MBR 预筛）官方未说明，需实测 EXPLAIN。

---

## 3. 空间索引【已查证，V4.3.1 创建索引文档】

创建语法（三种等价写法）：

```sql
-- 建表时
CREATE TABLE t1_g (
  id INT, name VARCHAR(18),
  g GEOMETRY NOT NULL SRID 4326,          -- 必须 NOT NULL + 列级 SRID
  SPATIAL INDEX tbl1_g_idx1(g)
);
-- 后建
CREATE SPATIAL INDEX idx ON t(g);
-- 或
ALTER TABLE t ADD SPATIAL INDEX idx(g);
```

关键限制（官方原文）：
1. 空间索引**仅支持局部索引，不支持全局索引**（分区表上是 local index）。
2. 索引列**必须 `NOT NULL`**。
3. 索引列**必须定义 SRID**（如 `SRID 4326`），否则索引"在后续查询时会无法生效"——因为 MBR 比较依赖坐标系。
4. 只能建在空间数据类型列上。
5. 索引内部**存储几何对象的 MBR（最小外接矩形）**做比较。官方文档**未出现"R-Tree"字样**【标注：MBR 索引已查证；是否为严格 R-Tree 未核实，MySQL 系通常是 R-Tree，OB 应一致】。

适用查询：基于空间谓词（ST_Contains/ST_Within/ST_Intersects/_ST_Dwithin 等）且几何列带列级 SRID 的过滤。精确形状计算仍是算子内计算，索引只做 MBR 粗筛。

---

## 4. 向量类型与向量索引

### 4.1 VECTOR 类型【已查证】

- 语法：`embedding VECTOR(3)`，**建列时必须指定维度**。
- 元素为单精度浮点数，不允许 NaN/Inf。
- **存储最大维度 16000**（V4.3.3 What's New 原文；V4.4.1 概述同）；支持稠密与稀疏向量。
- 写入字面量：`'[1.2,0.7,1.1]'`。

### 4.2 版本演进【已查证】

| 版本 | 向量能力 |
|---|---|
| V4.3.3 | MySQL 模式首发向量类型+索引（**实验特性**）；存储 16000 维 Float；HNSW 索引**最大 2000 维**；索引距离算法仅 **L2 / inner_product** |
| V4.4.0 | HNSW 索引最大维度提升至 **4096**；新增 **HNSW_SQ / HNSW_BQ / IVF_flat / IVF_PQ** 索引类型；距离算法新增 **cosine**；**明确支持"标量条件 + 空间关系（ST_Intersects 等）"作为向量查询过滤条件** |
| V4.4.1 | 向量搜索**默认启用**（`ob_vector_memory_limit_percentage` 默认 0=自适应：内存≤8GB 自适应 40%，>8GB 自适应 50%） |
| V4.5.0 | HNSW 系列索引参数文档化；与 Jina AI 集成 |

### 4.3 建索引 DDL【已查证，V4.3.3 / V4.4.0】

```sql
-- 建表时（V4.4.0 参数全集）
CREATE TABLE t1 (
  id INT PRIMARY KEY,
  doc VARCHAR(200),
  embedding VECTOR(3),
  VECTOR INDEX idx1(embedding) WITH (
    distance=L2,            -- 必填：l2 | inner_product | cosine（4.3.3 仅 l2/inner_product）
    type=hnsw,              -- 必填：hnsw | hnsw_sq | hnsw_bq（4.4.0+）
                            --        ivf_flat | ivf_pq（4.4.0+，实验特性）
    lib=vsag,               -- 默认 vsag
    m=16,                   -- HNSW 最大邻居数，默认 16，4.3.3 范围[5,64] / 4.4.0 范围[5,128]
    ef_construction=200,    -- 构建候选集，默认 200，范围[5,1000]，必须 > m
    ef_search=64            -- 搜索候选集，默认 64，范围[1,1000]
  )
);
-- 后建
CREATE VECTOR INDEX vec_idx1 ON vec_table(c2) WITH (distance=l2, type=hnsw);
-- 删除
DROP INDEX vec_idx1 ON vec_table;
```

注意：索引名长度有软限制（HNSW ≤ 25 字符、IVF ≤ 33 字符，V4.4.0 原文提示）；HNSW/HNSW_SQ/HNSW_BQ 是内存索引需完整载入内存；IVF 系列不占常驻内存。

### 4.4 ANN 查询 SQL【已查证，V4.3.3/V4.4.0/V4.4.1】

```sql
SELECT id, doc FROM t1
ORDER BY l2_distance(embedding, '[0.9, 1.0, 0.9]')
APPROXIMATE          -- 必须写 APPROXIMATE 或 APPROX 才走向量索引
LIMIT 3;
```

距离函数名（V4.4.1 SELECT 文档 + pyobvector SDK 文档双重核实）：
- `l2_distance(col, vec)` — L2 欧氏距离
- `cosine_distance(col, vec)` — 余弦距离
- `negative_inner_product(col, vec)` — 负内积（SDK 侧另有 `inner_product`）

语法硬约束（官方原文）：
- 必须带 `APPROXIMATE/APPROX`；不带则不走向量索引（也不报错，退化为全表精确计算）。
- 必须有 `ORDER BY` + `LIMIT`；`ORDER BY` 只支持**单个**向量条件；`LIMIT+OFFSET ∈ (0, 16384]`。
- 距离函数必须与索引 distance 匹配，否则不走索引不报错；调用不支持的距离函数会报错。
- **没有 PG 的 `<=>` 算子**【标注：官方文档未出现 `<=>`，函数式调用为唯一写法】。

### 4.5 向量索引维护【已查证】

- `ALTER SYSTEM SET ob_vector_memory_limit_percentage = 30;`（4.4.1 之前必须手动开；之后自适应）。
- 增量数据超过基线 20% 时每 24h 自动全量重建；可手动 `CALL dbms_vector.rebuild_index('idx','t','col');`
- 可调 `DBMS_VECTOR.SET_ATTRIBUTE` 设置重建并行度/触发阈值/周期。

---

## 5. 空间 + 标量 + 向量混合查询

### 5.1 官方明确的事实【已查证，V4.4.0 创建向量索引文档原文】

> "支持带有过滤条件的向量查询。过滤条件可以是标量类型的条件，**可以是空间关系，如 ST_Intersects 等**。暂不支持多值索引/全文索引/全局索引作为预过滤器。"
> "`WHERE` 条件会作为向量索引搜索**后的过滤条件**。"

即：**一条 SQL 内"向量 ANN（APPROXIMATE LIMIT K）+ WHERE 标量条件 + WHERE 空间谓词"是官方支持的写法**，语义是：先走 HNSW 向量召回，再用 WHERE（含空间谓词）后过滤。

### 5.2 可运行风格的混合 SQL 示例（基于官方语法拼装）

```sql
SELECT poi_id, name, rating,
       l2_distance(embedding, '[0.9,1.0,0.9]') AS vec_dist
FROM poi
WHERE
  -- 标量条件
  rating >= 4.5
  AND city = '宜宾'
  -- 空间范围条件（点 5km 圈；g 列带 NOT NULL SRID 4326）
  AND ST_Within(g, ST_Buffer(ST_GeomFromText('POINT(104.6 28.8)', 4326), 0.05))
  -- 或用 OB 私有距离谓词（坐标系单位，需换算）
  -- AND _ST_Dwithin(g, ST_GeomFromText('POINT(104.6 28.8)', 4326), 0.05)
ORDER BY l2_distance(embedding, '[0.9,1.0,0.9]')
APPROXIMATE
LIMIT 10;
```

### 5.3 风险标注（必须赛题环境实测）

1. **"后过滤"语义的坑**：官方明确 WHERE 是向量召回之后再过滤。若空间/标量过滤很强（比如 100 万条里只剩 20 条），APPROXIMATE LIMIT 10 可能向量侧只召回了前 K=10 条，过滤后不足 10 条甚至为 0。**生产上常用技巧：放大 LIMIT（如先召回 50~100 条再过滤）**，或接受近似性。【机制已查证；放大 LIMIT 的做法是工程经验，非官方明文】
2. **一条 SQL 是否同时使用"空间索引 + 向量 HNSW 索引"**：官方文档**未明确**优化器能否同时利用两个索引。按官方表述，向量路径下 WHERE 只是"后过滤条件"，空间谓词很可能走的是召回后逐行计算（或空间索引辅助），**不保证双索引并行**。【未核实，必须 `EXPLAIN` 实测】
3. `_ST_Dwithin` 的距离单位随坐标系（经纬度 SRS 下单位是"度"不是米！）；要查"球面 5km"应考虑 `ST_Distance_Sphere` 或先 `ST_Transform` 到投影坐标系。【单位注意事项已查证函数文档】
4. HNSW_BQ 索引 distance 仅支持 l2【已查证】。
5. 分区表上空间索引只能 local index【已查证】；向量索引在分区表的行为【未核实】。

---

## 6. 分布式 / 事务与版本/发行版差异

- **分布式**：原生分布式架构，水平扩展、多分区支撑海量向量；向量索引集成 VSAG 算法库（官方称 960 维 GIST 数据集 ANN-Benchmarks 表现好）。【已查证，V4.4.1 向量搜索概述】
- **事务/高可用**：基于 Multi-Paxos；HNSW 内存索引在主备/跨机房容灾切换后可实时访问；分布式事务保证向量数据一致性。【已查证】
- **向量首发版本**：V4.3.3，MySQL 模式，实验特性【已查证】。
- **社区版 vs 企业版**：demo 仓库 README 明确给出社区版 Docker 镜像 `oceanbase/oceanbase-ce`（标注 "full features"）可跑该多模 demo；`seekdb` 是精简版。官方文档未单列"企业版独有向量功能"清单【未核实差异明细】，按 demo 指向社区版可用。

---

## 7. 与 PostGIS 的能力差异（基于官方文档 + demo 定位）

| 维度 | OceanBase（MySQL 模式 4.4.x） | PostGIS |
|---|---|---|
| 类型/语法 | MySQL GIS 语法族（GEOMETRY/ST_*），列级 SRID | PostgreSQL/PostGIS 语法族，SDO 无 |
| 函数完备度 | 关系/属性/运算为主；**有 PG 兼容的 `_ST_*` 私有函数组**（_ST_Dwithin/_ST_MakeEnvelope/_ST_AsMVTGeom 等） | 函数库最完整（含 ST_Union 聚合、栅格、routing 等） |
| 空间聚合 | **未见 ST_Union_Agg 类聚合函数**【未核实/大概率无】 | 有 ST_Union(agg)、ST_Collect 等 |
| 索引 | MBR 空间索引（局部、必须 NOT NULL+SRID） | GIST/SP-GiST，无强制 NOT NULL SRID 列级约束 |
| 一体化 | **同库同时有 SPATIAL INDEX + VECTOR INDEX(HNSW) + 全文索引 + 标量索引，一条 SQL 混合** | 需 pgvector 扩展拼配，无原生向量 HNSW（pgvector 是 IVFFlat/HNSW 扩展） |
| 定位 | 官方主打"多模一体、原生分布式、MySQL 兼容"，替代 MySQL+ES+向量库多件套 | 专业 GIS 计算引擎，函数深度与生态更强 |

结论：GeoMind 不应追求"PostGIS 全套函数"，而应主打 OB 独有的**单库三模混合（空间+标量+向量一条 SQL）** 叙事——这正是赛题"高德赛道"的评分点。

---

## 8. 高德文旅小助手教程详解

### 8.1 教程存在性：已找到（官方两篇 + GitHub 仓库）

1. **交互式教程 V1.0.0**（2026-08-17 更新）：https://www.oceanbase.com/docs/common-interactive-tutorials-1000000002963909
   —— 页面为前端动态渲染，直接抓取正文失败；通过搜索快照+seekdb 镜像+GitHub 仓库交叉还原了其内容。
2. **文档版 V4.5.0**：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000004475454《基于 OceanBase 多模融合构建文旅小助手》
3. **OB Cloud 控制台交互式**：https://www.oceanbase.com/docs/enterprise-oceanbase-cloud-1000000002963136
4. **demo 仓库**：`git clone https://github.com/oceanbase-devhub/ob-multi-model-search-demo.git`（现重定向为 **https://github.com/oceanbase-labs/TravelAssist**，Apache-2.0）

### 8.2 架构（已查证）

```
Kaggle「China City Attraction Details」CSV 数据集
   │
   ▼  attraction_data_preprocessor.py（数据预处理）
高德开放平台「基础 LBS 服务」API（AMAP_API_KEY）── 地理/逆地理编码补全经纬度
   │
   ▼  BGE-m3 模型把景点文本 embedding 成向量
OceanBase（MySQL 租户）：
   ├── 标量列：景点评分、城市、季节偏好……
   ├── 空间列：POINT（SRID 4326，高德经纬度）
   └── 向量列：VECTOR(BGE-m3 维度) + VECTOR INDEX(HNSW)
   │
   ▼  Streamlit UI（src/ui.py，http://localhost:8501）
通义千问 Qwen（DASHSCOPE_API_KEY，阿里云百炼）LLM Agent：多轮对话/任务分解
```

- 技术栈：Python 3.10+ / Poetry（旧版）→ 新版 `uv sync`；`pyobvector==0.2.16`；Streamlit。
- 部署：`docker compose up -d`；`DB_STORE=seekdb | oceanbase` 切换后端；社区版镜像 `oceanbase/oceanbase-ce`。
- 教程步骤（官方快照）：① 获取数据库连接串 → ② 获取 LLM API Key（阿里云百炼）→ ③ 获取高德基础 LBS 服务 API Key → ④ 下载 Kaggle 数据集 → ⑤ clone 仓库、配 `.env`（`OB_URL/OB_USER/OB_PWD/OB_DB_NAME/DASHSCOPE_API_KEY/AMAP_API_KEY`）→ ⑥ 跑 `attraction_data_preprocessor.py` 导数据 → ⑦ `streamlit run ui.py` 启动聊天。

### 8.3 表结构与混合 SQL

- **官方教程文档未在公开页展示完整建表 DDL 与混合查询 SQL 原文**（动态渲染 + 正文在仓库 `src/` 内）。【标注：以下为仓库结构推断，需 clone 后精读 `src/` 下的建表与 query 模块】
- 可确认的设计要素：一行景点记录同时含"标量属性 + POINT 空间列 + VECTOR 向量列"，混合查询走"空间过滤 + 语义向量召回 + LLM 生成推荐"。
- 高德 API 边界：仅用**基础 LBS 服务**（地理编码/逆地理编码，把地名→经纬度；逆地理可返回周边 POI/AOI）。教程未涉及高德路径规划/路线 API。

---

## 9. 对 GeoMind 存储设计的建议

1. **空间列**：`geom POINT NOT NULL SRID 4326`（高德经纬度即 WGS84 系，SRID 4326 直接匹配），建 `SPATIAL INDEX`；任何要走空间索引的列都必须 `NOT NULL + SRID`。
2. **向量列**：`embedding VECTOR(<dim>)`（BGE-m3 是 1024 维，< 4096 上限，安全）；HNSW 索引 `distance=cosine, type=hnsw, m=16, ef_construction=200`；赛题版本若 ≥4.4.0 可用 cosine，若停在 4.3.3 只能 l2/inner_product，**部署时先确认版本**。
3. **混合查询按官方"后过滤"模型写 SQL**：`WHERE 标量 + 空间谓词 ORDER BY l2_distance/cosine_distance APPROXIMATE LIMIT k`；k 适当放大（50~100）再截断展示，缓解后过滤召回不足。
4. **不要迷信 ST_Union_Agg / MBRContains**：官方未列出，赛题里别写；空间聚合需求改应用层。
5. **球面距离**：POI 圈选优先 `_ST_Dwithin`（注意单位是"度"，需换算）或 `ST_Distance_Sphere`（仅 Point 间，返回米）；要"5km 圈"建议先 ST_Buffer + 度换算，或实测。
6. **答辩叙事**：主打"同库三模——SPATIAL INDEX(RTree/MBR) + VECTOR INDEX(HNSW) + B-tree 标量索引，一条 SQL 完成高德 POI 的空间范围 + 语义推荐"，并引用官方教程（TravelAssist 仓库）作为可行性佐证。
7. 上线前 `EXPLAIN` 实测三件事：① APPROXIMATE 是否命中 HNSW；② 空间谓词是否走 SPATIAL INDEX；③ 双索引是否被同一 SQL 同时利用（官方未承诺）。

---

## 10. 参考链接清单

**官方空间函数/类型**
- 空间关系函数 V4.3.0：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000644024
- 空间运算函数 V4.3.0：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000644025
- 几何属性函数 V4.2.2：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000511875
- 空间运算函数 V4.2.2（_ST_*）：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000511877
- 创建空间列 V4.3.3：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000001433667
- 创建索引 V4.3.1（SPATIAL INDEX）：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000820750
- V4.3.1 What's New（GIS 增强）：https://en.oceanbase.com/docs/common-oceanbase-database-10000000001375280

**官方向量**
- 创建向量索引 V4.3.3：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000001484244
- 创建向量索引 V4.4.0（含过滤条件/混合）：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003378338
- 向量搜索快速上手 V4.4.1：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003976681
- 向量搜索概述 V4.4.1：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003976682
- SELECT 向量距离函数 V4.4.1：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003981642
- V4.3.3 What's New（向量实验特性）：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000001509391

**文旅教程**
- 交互式教程：https://www.oceanbase.com/docs/common-interactive-tutorials-1000000002963909
- 文档版 V4.5.0：https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000004475454
- demo 仓库：https://github.com/oceanbase-labs/TravelAssist
- 官方课程站（未逐字打开）：https://open.oceanbase.com/course/850
