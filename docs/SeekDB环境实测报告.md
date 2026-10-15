# SeekDB 环境实测报告（GeoMind 阶段 0）

> 实测时间：2026-10-15
> 当前环境：**Docker 容器化 SeekDB V1.4.0**（Windows + Docker Desktop，WSL2 后端）
> 历史环境：Windows 原生 SeekDB V1.3.0（已停止、保留可回滚，见 §6）
> 结论用途：作为《GeoMind 架构设计文档》第 6 章 OceanBase 存储设计的**实测修正依据**；凡本文与架构文档冲突处，以本文实测结果为准。

---

## 1. SeekDB 是什么（官方定位）

**OceanBase SeekDB = OceanBase 官方推出的 AI 原生混合搜索数据库（AI-native search database），开源免费。**

官方定义：SeekDB 在单引擎内统一了五种数据模型——**关系（标量）/ 向量 / 全文 / JSON / GIS 空间**，正是本赛题"空间 + 标量 + 向量多模混合检索"的官方载体。

- 官网：www.seekdb.ai / www.oceanbase.ai；文档：docs.seekdb.ai；仓库：github.com/oceanbase/seekdb
- 最新版：**V1.4.0（2026-08-27）**，二进制 -63.9%、稳态内存 -73.6%、CPU -70.3%，Rust 重写 MySQL 协议栈
- 平台支持：官方正式发布 **Linux（rpm/deb）+ macOS（pkg）**；Windows 官方推荐 **Docker 服务器模式**（嵌入式不支持 Windows）。**V1.4.0 没有 Windows 原生 exe 安装包**（V1.3.0 的原生 exe 是当时的实验特性）。

---

## 2. 本地实例信息（V1.4.0 / Docker）

| 项目 | 值 |
| --- | --- |
| 部署方式 | Docker Desktop（Windows + WSL2），容器名 `seekdb`，开机自启 `--restart unless-stopped` |
| 镜像 | `quay.io/oceanbase/seekdb:latest`（digest `sha256:4db4961869d5…`，构建 2026-10-10，底层 Rocky Linux 9） |
| **内核版本** | `VERSION()`=`5.7.25-OceanBase seekdb-v1.4.0.0`；`version_comment`=`OceanBase 4.3.5.3 seekdb (r1.4.0.0) … Aug 26 2026` |
| 监听端口 | 容器 `2881`（MySQL 协议）→ 宿主机 `127.0.0.1:2881` |
| 连接用户 | `root`，**空密码**（未设 `ROOT_PASSWORD`） |
| 规格 | `MEMORY_LIMIT=2G`，`cpu_count=4`（容器启动脚本默认） |
| 数据持久化 | Docker named volume `seekdb_data` → 容器内 `/var/lib/seekdb`（含 run/etc/store 与 `.initialized` 标记） |
| 配置文件 | 容器内 `/etc/seekdb/seekdb.cnf` |
| 客户端 | 本机 `D:\mysql\mysql-8.4.8-winx64\bin\mysql.exe`，连接需加 `--ssl-mode=DISABLED` |

**连接命令（Windows 侧，与升级前完全一致）：**

```
& "D:\mysql\mysql-8.4.8-winx64\bin\mysql.exe" -h 127.0.0.1 -P 2881 -uroot --ssl-mode=DISABLED
```

**容器启动命令（已执行，记录备查）：**

```powershell
docker volume create seekdb_data
docker run -d --name seekdb --restart unless-stopped `
  -p 2881:2881 `
  -v seekdb_data:/var/lib/seekdb `
  -e MEMORY_LIMIT=2G `
  quay.io/oceanbase/seekdb:latest
```

> 镜像拉取说明：Docker Hub `oceanbase/seekdb` 在本机直连超时，改用官方备用仓库 **`quay.io/oceanbase/seekdb`** 拉取成功。

**容器镜像支持的环境变量（来自 entrypoint `/root/start.sh`）：**

- `ROOT_PASSWORD`：root 密码（不设则保持空密码）
- `SEEKDB_DATABASE`：首次初始化时自动创建的库
- `INIT_SCRIPTS_PATH`：首次初始化时自动执行的 `.sql` 目录（可用于一键灌种子数据）
- `MEMORY_LIMIT` / `CPU_COUNT` / `DATAFILE_SIZE` / `DATAFILE_NEXT` / `DATAFILE_MAXSIZE` / `LOG_DISK_SIZE`
- 首次初始化完成后写 `.initialized`；之后重启复用数据，且**不会重置**密码

---

## 3. 能力实测矩阵（V1.4.0 全部已实跑复验）

| 能力 | 语法 | 结果 |
| --- | --- | --- |
| 空间类型 | `POINT NOT NULL`（SRID 0）、`POLYGON` | ✅ |
| 空间索引 | `SPATIAL KEY idx (col)` | ✅ |
| 空间构造/输出 | `ST_GeomFromText`、`ST_AsText`、`ST_X/Y`、`ST_SRID` | ✅ |
| 球面距离 | `ST_Distance_Sphere(g1,g2)` 返回**米**（SRID 0 下也正确） | ✅（A=0/B=1478.8m/C=1642445m） |
| 距离圈选 | `_ST_Dwithin(g1,g2,d)`（OB 私有，**d 单位是度**） | ✅ |
| 空间关系/缓冲 | `ST_Intersects`、`ST_Buffer` | ✅ |
| **几何交集/聚合** | `ST_Intersection`、`ST_Union_Agg` | ❌ **V1.4 确认函数不存在（ERROR 1305）** |
| 向量类型 | `VECTOR(n)`，已验证 n=8 与 **n=1024（BGE-m3 可直接用）** | ✅ |
| 向量索引 | `VECTOR KEY … WITH (DISTANCE=COSINE,TYPE=HNSW,M,EF_CONSTRUCTION,EF_SEARCH)` | ✅ |
| cosine 距离 | `cosine_distance()` + cosine HNSW | ✅ |
| ANN 查询 | `ORDER BY cosine_distance(emb,:q) APPROXIMATE LIMIT k` | ✅（cafe 0.0 / tea 0.0196 / garage 0.5694） |
| ANN 执行计划 | EXPLAIN = `VECTOR INDEX SCAN` | ✅ 索引命中 |
| JSON | `JSON` 列、`JSON_EXTRACT`、`->` | ✅ |
| 全文检索 | `FULLTEXT KEY … WITH PARSER ngram` + `MATCH … AGAINST`（中文） | ✅（"咖啡" 命中，score 0.240） |
| 平面距离 | `ST_Distance(g1,g2)` 返回**度** | ✅（0.00050 度 ≈ 51 m，与球面距离一致） |
| 包含 / 在内 | `ST_Contains(poly,pt)`、`ST_Within(pt,poly)` | ✅（5 行样本各命中 4） |
| 质心 / 面积 | `ST_Centroid`、`ST_Area` | ✅（POINT(104.05 30.65) / 0.01 平方度） |
| **空间连接 + 分组聚合** | `t_grid JOIN t_poi ON ST_Contains(g,location)` + `GROUP BY` | ✅ 功能跑通（3/1/1，均值正确）；⚠️ EXPLAIN = `NESTED-LOOP JOIN` + 双侧 `TABLE FULL SCAN`，**未走 SPATIAL INDEX** |
| **注册 SRID 4326** | `CREATE SPATIAL REFERENCE SYSTEM 4326 …` | ❌ **ERROR 1064 语法不支持**（OB 未实现该语句）→ 确定统一 SRID 0 |

---

## 4. 关键实测：三模混合查询（赛题核心，V1.4 复验通过）

一条 SQL 同时完成 **空间圈选（_ST_Dwithin）+ 标量过滤（status/cat）+ 语义向量召回（cosine HNSW APPROXIMATE）**：

```sql
SELECT id, name, rating,
  ROUND(ST_Distance_Sphere(location, :anchor),0) AS dist_m,
  ROUND(cosine_distance(emb, :qvec),4) AS vd
FROM t_hybrid
WHERE status=1 AND cat=1
  AND _ST_Dwithin(location, :anchor, 0.05)
ORDER BY cosine_distance(emb, :qvec) APPROXIMATE LIMIT 5;
```

实测正确返回 A（0m）、B（1331m），自动排除：停业点（status=0）、异业点（cat=2）、圈外点（E 在 1600km 外）。

**执行计划算子（V1.4 与 V1.3 一致，是关键结论）：**

```
|1| └─SUBPLAN FILTER                            |
|2|   ├─VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER) | t_hybrid   filter([status=1],[cat=1],[_ST_Dwithin(...)])
|3|   └─TABLE GET                               | t_hybrid   (回表取 location)
```

SeekDB 提供 `VECTOR INDEX ADAPTIVE SCAN (PRE-FILTER)`：向量索引扫描时做**预过滤**，比"仅召回后过滤"更先进。架构文档第 6.5 节两种写法的结论保持不变（优化器自适应，两阶段写法仅作极小圈选场景兜底）。

---

## 5. V1.4 与 V1.3 的差异（架构与代码必须注意）

1. **版本与部署**：V1.3.0（Windows 原生 exe，实验特性）→ **V1.4.0（Docker 容器，官方推荐）**。连接串、端口、账号密码均不变，应用层零改动。

2. **DDL 不接受 `REPLICA_NUM`（V1.4 重要变化，会直接报错）**：V1.3 的 mysqldump 导出语句含 `REPLICA_NUM = 1`，在 V1.4 上——
   - 库级 `CREATE DATABASE … REPLICA_NUM = 1` → `ERROR 1064`
   - 表级 `CREATE TABLE … REPLICA_NUM = 1` → `ERROR 1064`
   - 单机版已移除该分布式选项。**V1.4 建库/建表 DDL 一律不写 `REPLICA_NUM`，也不写 `ORGANIZATION INDEX / TABLET_SIZE / USE_BLOOM_FILTER / ENABLE_MACRO_BLOCK_BLOOM_FILTER / BLOCK_SIZE` 等由 mysqldump 带出的分布式存储选项。** 向量索引里的 `LIB=VSAG` 可省略（默认即为 vsag）。

3. **cosine + 1024 维继续可用**：V1.4 保持，无需降级 l2。

4. **SRID 4326 仍未预置（V1.4 未改善）**：建表写 `POINT NOT NULL SRID 4326` 仍报 `ERROR 3548 (SR001): Spatial reference system is empty`。方案：**列不声明 SRID（默认 SRID 0）存 WGS84 经纬度，`ST_Distance_Sphere` 仍返回正确米数**；DDL 与代码注释统一标注"坐标按 WGS84 经纬度、SRID 0 存储"。

5. **空间叠加函数确认缺失**：`ST_Intersection`、`ST_Union_Agg` 在 V1.4 仍 `FUNCTION does not exist`（2026-10-15 复测 `ST_Union_Agg` → ERROR 1305）。**交集/合并类多边形运算不能下推数据库**，需在应用层（如 Shapely）或高德侧完成。
   **可下推空间算子白名单（本次复测扩充）**：距离 `ST_Distance_Sphere`（米）、`ST_Distance`（度）、圈选 `_ST_Dwithin`（度）、布尔关系 `ST_Intersects`/`ST_Contains`/`ST_Within`、缓冲 `ST_Buffer`（度）、`ST_Centroid`、`ST_Area`。

6. **全文是第五模**：ngram 全文检索稳定，应在 L4 QueryBuilder 中补全文谓词，与空间/向量召回互补。

---

## 6. 升级与迁移记录（V1.3.0 → V1.4.0，可回滚）

官方不支持原地升级，采用 **mysqldump 逻辑迁移**。本次实际步骤：

1. **备份**：`mysqldump -h 127.0.0.1 -P 2881 -uroot --ssl-mode=DISABLED --skip-triggers --hex-blob --databases geomind`
2. **停旧服务并改为手动**（避免开机抢占 2881；未删除、可回滚）：
   `Stop-Service seekdb; Set-Service seekdb -StartupType Manual`
3. **拉镜像并启动容器**（见 §2 命令）
4. **迁移 DDL**：V1.3 dump 去掉全部 7 处 `REPLICA_NUM = 1` 后仍含其他分布式表选项，V1.4 报错；改用**干净 DDL 重建 6 张测试表结构 + 只导 INSERT 数据**（迁移期脚本总 29.2 KB，已于 2026-10-15 确认无保留价值后删除）
5. **复验**：五模能力、三模混合、EXPLAIN 算子、容器重启数据持久化，全部通过

**迁移产物去向（2026-10-15 清理）**：迁移期产物（`geomind_v130.sql`、`v140_schema.sql`、`v140_data.sql`、`import_v140.sql`、各阶段 err 日志、`service_switch.log` 等共 13 个文件 / 29.2 KB）确认为一次性中间物，已全部删除，`backup/` 目录随之移除。
**保留一项**：`v140_verify.sql`（五模 + 三模混合 7 项复验脚本）迁至 **`docs/sql/verify_v140.sql`**，需要复跑时直接执行即可。
**项目正式建表**：以《GeoMind 架构设计文档》§6.3 的 DDL 为准（已按 V1.4 实测口径校验），不再依赖迁移期脚本。

**回滚方案（保留 V1.3.0）：**
- 原 V1.3.0 二进制（`D:\seekdb\app\bin\seekdb.exe`）与数据目录（`D:\seekdb\data`）**完整保留未删**；
- 回滚：`docker stop seekdb` → 管理员启动原服务（`Start-Service seekdb`，已改手动），即恢复 V1.3.0；
- 两套实例共用 2881 端口，**同一时刻只能启动一个**。

---

## 7. 日常运维命令（Docker）

```powershell
docker start seekdb            # 启动（Docker Desktop 运行时）
docker stop seekdb             # 停止（容器会走 USR1 优雅停库）
docker restart seekdb          # 重启
docker logs -f seekdb          # 跟踪启动/初始化日志（等待 "Seekdb started"）
docker ps --filter name=seekdb # 查看容器状态
docker exec -it seekdb sh      # 进入容器（数据目录 /var/lib/seekdb）
docker volume inspect seekdb_data   # 查看持久化卷实际位置
```

> 自启前提：容器 `--restart unless-stopped` 仅在 **Docker Desktop 已启动** 时生效；请确认 Docker Desktop 配置为开机自启（Settings → General → Start Docker Desktop when you log in）。

**逻辑备份（V1.4，与 V1.3 相同）：**
```powershell
mysqldump -h 127.0.0.1 -P 2881 -uroot --ssl-mode=DISABLED --skip-triggers --hex-blob --databases geomind > backup.sql
```

---

## 8. 待办（阶段 0 收尾 / 后续）

- [ ] 用 10 万级以上数据验证 SPATIAL INDEX 与 HNSW 的真实命中与召回/延迟（当前 3~5 行样本走全表属正常）
- [x] **SRID 4326 注册：已实测不可行**——`CREATE SPATIAL REFERENCE SYSTEM 4326 …` 报 **ERROR 1064 语法不支持**（OB 未实现该语句），导入 `spatial_ref_sys` 亦无内置入口。结论固化：**DDL 一律 SRID 0 + WGS84 经纬度存值**，该待办关闭
- [x] **空间连接下推：功能已跑通**——`t_grid JOIN t_hybrid ON ST_Contains(g,location)` + `GROUP BY` 实测返回正确聚合（grid1=3 均值 4.43 / grid2=1 / grid3=1）
- [ ] 空间连接**索引加速未验证**：上述连接 EXPLAIN = `NESTED-LOOP JOIN` + 两侧 `TABLE FULL SCAN`，**优化器未选 SPATIAL INDEX 驱动连接**；需 10 万级数据复验（若仍全扫，空间聚合改为「`_ST_Dwithin` 预筛 + 应用层精确归属」或「网格 ID 冗余列 + 普通索引 JOIN」）
- [ ] 交集/合并类多边形运算放到应用层（Shapely）或高德，勿在 SeekDB 调用 `ST_Intersection/ST_Union_Agg`
- [ ] 项目正式 DDL 以《GeoMind 架构设计文档》§6.3 为准，禁止携带 `REPLICA_NUM` 等分布式选项（`backup/` 已清理，迁移期脚本不再作为模板来源）
- [ ] 据本文把《GeoMind 架构设计文档》第 6 章与部署章节统一更新为 **SeekDB V1.4.0 / Docker**
