"""L4 Query Builder：唯一的 SQL 出口 + 三层卡点。

架构约束（§3.2 要点①、§13 风险"LLM 生成 SQL 正确性"）：
  LLM 永不拼裸 SQL。所有 SQL 由本模块模板化编译产生，参数一律 %(name)s 占位。

三层卡点（任一失败即拒绝执行，不静默）：
  1. 静态校验 validate()   —— 单语句 / 只读 / 禁 SELECT * / 列 ∈ schema 白名单 /
                              空间谓词必须配对 MBR / 语义排序必须 APPROXIMATE + LIMIT ≤ max
  2. dry-run                —— LIMIT 1 包装执行
  3. EXPLAIN 算子断言       —— 计划必须命中 expected_operators，否则抛 PlanAssertionError
                              （上层可捕获后降级到写法 B 两阶段，见架构 §6.6）

压测结论的代码级固化（docs/压测报告-10万级.md）：
  SPATIAL INDEX 在 V1.4 完全不参与查询计划 → 空间查询必须两段式：
    lon BETWEEN .. AND lat BETWEEN ..（走 KEY(lon,lat)）+ ST_* 精确复核。
  compile 阶段就强制：只要出现空间谓词，产物必含 MBR，否则 CompileError。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from backend.config import Settings, get_settings
from backend.db.connection import OBConnection

# ---------------------------------------------------------------------------
# 错误类型（带原因码，上层可据此降级）
# ---------------------------------------------------------------------------


class CompileError(ValueError):
    """编译期错误：约束组合不合法（如空间谓词缺 MBR）。"""


class SQLValidationError(ValueError):
    """静态校验失败。code 供上层分类处理。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"[{code}] {detail}")
        self.code = code
        self.detail = detail


class DryRunError(RuntimeError):
    """dry-run 执行失败。"""


class PlanAssertionError(RuntimeError):
    """EXPLAIN 未命中期望算子。携带实际计划，供上层降级决策。"""

    def __init__(self, expected: tuple[str, ...], actual_plan: str) -> None:
        super().__init__(f"plan mismatch: expected one of {expected}")
        self.expected = expected
        self.actual_plan = actual_plan


# ---------------------------------------------------------------------------
# 入参 schema（四类约束）
# ---------------------------------------------------------------------------


class SpatialConstraint(BaseModel):
    """空间约束。center+radius_m 或 polygon_wkt 二选一。"""

    center: tuple[float, float] | None = Field(default=None, description="(lon, lat) WGS84")
    radius_m: float | None = Field(default=None, gt=0, le=100_000)
    polygon_wkt: str | None = Field(default=None, description="等时圈/框选 WKT，SRID 0")
    time_bucket: str | None = None

    @field_validator("center")
    @classmethod
    def _check_center(cls, v: tuple[float, float] | None) -> tuple[float, float] | None:
        if v is None:
            return None
        lon, lat = v
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("center 超出经纬度范围")
        return v


class ScalarConstraint(BaseModel):
    category_id: int | None = None
    min_rating: float | None = Field(default=None, ge=0, le=5)
    status: int = Field(default=1, ge=0, le=2)
    attrs_filter: dict[str, Any] | None = Field(default=None, description="JSON 属性等值条件")


class SemanticConstraint(BaseModel):
    keywords: list[str] = Field(default_factory=list, description="ngram 全文词，AND 语义")
    query_text: str | None = Field(default=None, description="语义查询文本，经 embedding 转向量")
    limit: int = Field(default=10, ge=1, le=200)


class GridAggSpec(BaseModel):
    """网格聚合入参（spatial_join_agg / traffic_grid_agg 共用）。

    grid_ids 直接给，或给 center+radius_m 由后端按网格规格推导（二选一）。
    """

    grid_ids: list[int] | None = None
    center: tuple[float, float] | None = None
    radius_m: float | None = Field(default=None, gt=0, le=100_000)
    time_bucket: str | None = None
    category_id: int | None = None
    limit: int = Field(default=50, ge=1, le=500)


class PoiDetailSpec(BaseModel):
    poi_id: int = Field(ge=1)


class PoiNameSpec(BaseModel):
    """名称点查入参（地理编码库内兜底用，架构 §7「离线已入库 POI 走 OB」）。

    name 走 LIKE 参数化匹配：通配符与转义符在编译期转义，用户输入永不拼进 SQL 文本。
    """

    name: str = Field(min_length=1, max_length=40)
    city: str | None = Field(default=None, max_length=30)
    limit: int = Field(default=5, ge=1, le=50)


class PointQuerySpec(BaseModel):
    """主键批量点查入参（图层数据端点取坐标用，架构 §2.2 L4「唯一 SQL 出口」）。

    ids 上限 200 与 `SemanticConstraint.limit` 同一口径（受 validate() 的
    max_limit 天花板约束）；列名不作为参数传入——只可能来自 `_POINT_COLUMNS`
    常量映射，标识符注入面归零。
    """

    table: Literal["dim_poi", "fact_traffic_grid"]
    ids: list[int] = Field(min_length=1, max_length=200, description="主键列表（自动去重）")
    time_bucket: str | None = Field(default=None, max_length=32, description="仅网格表")


class HybridQuerySpec(BaseModel):
    spatial: SpatialConstraint = Field(default_factory=SpatialConstraint)
    scalar: ScalarConstraint = Field(default_factory=ScalarConstraint)
    semantic: SemanticConstraint = Field(default_factory=SemanticConstraint)
    target: Literal["dim_poi", "fact_store", "knowledge_doc"] = "dim_poi"


# ---------------------------------------------------------------------------
# 编译产物
# ---------------------------------------------------------------------------


@dataclass
class CompiledQuery:
    """编译产物：SQL 自带声明，供校验层逐条断言。

    columns 是**从 SQL 文本中提取出的已知列名**（_extract_columns），
    不是 SELECT 项 —— 校验层据此确认没有跨表串列。
    """

    sql: str
    params: dict[str, Any]
    target: str
    columns: tuple[str, ...] = field(default_factory=tuple)
    tables: tuple[str, ...] = field(default_factory=tuple)
    has_spatial: bool = False
    has_semantic: bool = False
    expected_operators: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> str:
        return (f"target={self.target} spatial={self.has_spatial} semantic={self.has_semantic} "
                f"expect={self.expected_operators}")


# ---------------------------------------------------------------------------
# 目标表的列白名单（与 backend/db/migrations/001_init_schema.sql 一致）
# 说明：这些是本模块唯一允许编译进 SQL 的列；新增列必须同步此处。
# ---------------------------------------------------------------------------
_TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "dim_poi": (
        "poi_id", "name", "category_id", "brand", "address", "city",
        "location", "lon", "lat", "grid_id", "rating", "attrs", "status", "emb",
    ),
    "fact_store": (
        "store_id", "name", "rent", "area_sqm", "streetfront_tier",
        "location", "lon", "lat", "grid_id", "attrs", "emb",
    ),
    "fact_traffic_grid": (
        "grid_id", "time_bucket", "grid_poly", "center_lon", "center_lat",
        "resident_cnt", "flow_cnt",
    ),
    "knowledge_doc": ("doc_id", "title", "text", "meta", "emb"),
    "dim_category": ("category_id", "name_l1", "name_l2", "name_l3"),
}

_READONLY_TABLES = frozenset(_TABLE_COLUMNS)

# 主键点查的列映射（表 → 主键列, 经度列, 纬度列）。列名只能来自这张表，
# 调用方传不进任何标识符——这是 compile_point_query 唯一的列来源。
_POINT_COLUMNS: dict[str, tuple[str, str, str]] = {
    "dim_poi": ("poi_id", "lon", "lat"),
    "fact_traffic_grid": ("grid_id", "center_lon", "center_lat"),
}

# 语义检索允许的距离列（向量目标表）
_VEC_ORDER: dict[str, str] = {
    "dim_poi": "cosine_distance(emb, %(qvec)s)",
    "fact_store": "cosine_distance(emb, %(qvec)s)",
    "knowledge_doc": "cosine_distance(emb, %(qvec)s)",
}
_VEC_DIST_SELECT: dict[str, str] = {
    "dim_poi": "ROUND(cosine_distance(emb, %(qvec)s),4) AS sem_dist",
    "fact_store": "ROUND(cosine_distance(emb, %(qvec)s),4) AS sem_dist",
    "knowledge_doc": "ROUND(cosine_distance(emb, %(qvec)s),4) AS sem_dist",
}
_SPHERE_SELECT: dict[str, str] = {
    "dim_poi": "ROUND(ST_Distance_Sphere(location, ST_GeomFromText(%(center_wkt)s)),0) AS dist_m",
    "fact_store": "ROUND(ST_Distance_Sphere(location, ST_GeomFromText(%(center_wkt)s)),0) AS dist_m",
}

# 全文列（MATCH 谓词挂载点）
_FT_COLUMN: dict[str, str] = {
    "dim_poi": "name",
    "fact_store": "name",
    "knowledge_doc": "text",
}

_FORBIDDEN_SQL = re.compile(r"\b(DROP|DELETE|UPDATE|INSERT|ALTER|TRUNCATE|CREATE|GRANT|REPLACE)\b", re.I)
_SELECT_STAR = re.compile(r"SELECT\s+\*", re.I)
# 关键词字符白名单：中英文数字，1-20 字。构造 '+kw' 时的唯一放行通道。
_KW_OK = re.compile(r"^[\u4e00-\u9fa5A-Za-z0-9]{1,20}$")

# 全部已知列名（跨表）。用于从 SQL 文本中提取"疑似列标识符"：
# 只有命中这个集合的标识符才需要按 target 白名单校验，函数名/关键字天然排除。
_ALL_KNOWN_COLUMNS = frozenset(c for cols in _TABLE_COLUMNS.values() for c in cols)
_IDENT = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")


def _extract_columns(sql: str, tables: str | tuple[str, ...]) -> tuple[str, ...]:
    """从 SQL 提取出现的已知列名；凡不属于给定表集合即视为跨表串列。

    tables 可以是单表名或表名元组（JOIN 查询必须传元组，否则另一张表的列
    会被误判——本项目实测踩过）。
    """
    if isinstance(tables, str):
        tables = (tables,)
    allowed: set[str] = set()
    for t in tables:
        allowed |= set(_TABLE_COLUMNS[t])
    found = sorted({m for m in _IDENT.findall(sql) if m in _ALL_KNOWN_COLUMNS})
    bad = [c for c in found if c not in allowed]
    if bad:
        raise SQLValidationError("UNKNOWN_COLUMN", f"列不属于 {list(tables)}: {bad}")
    return tuple(found)


class QueryBuilder:
    """把 HybridQuerySpec 编译成一条 SQL，并跑完三层卡点。"""

    def __init__(self, ob: OBConnection, settings: Settings | None = None) -> None:
        self.ob = ob
        self.s = settings or get_settings()

    # ------------------------------------------------------------------
    # 编译
    # ------------------------------------------------------------------
    def _compile_parts(self, spec: HybridQuerySpec, qvec: str | None) -> dict[str, Any]:
        """构建 WHERE / SELECT / params 等中间产物（compile_hybrid 与 compile_two_stage 共用）。

        qvec：语义向量字面量（由 EmbeddingProvider 生成）。spec.semantic.query_text
        非空但 qvec 为空时，说明调用方忘了转向量 —— 直接 CompileError（避免半成品 SQL）。
        """
        if spec.target not in _TABLE_COLUMNS:
            raise CompileError(f"未知 target: {spec.target}")

        has_semantic = bool(spec.semantic.query_text)
        if has_semantic:
            if not qvec:
                raise CompileError("semantic.query_text 非空但未提供 qvec（调用方需先转向量）")
            if qvec.count(",") + 1 != self.s.embedding_dim:
                raise CompileError(
                    f"qvec 维度 {qvec.count(',') + 1} != 配置 embedding_dim {self.s.embedding_dim}")

        params: dict[str, Any] = {}
        where: list[str] = []
        select_items: list[str] = []
        has_spatial = False

        # ---- 标量 ----
        sc = spec.scalar
        if spec.target in ("dim_poi", "fact_store"):
            where.append("status = %(status)s")
            params["status"] = sc.status
        if sc.category_id is not None and spec.target in ("dim_poi", "fact_store"):
            where.append("category_id = %(category_id)s")
            params["category_id"] = sc.category_id
        if sc.min_rating is not None and spec.target in ("dim_poi", "fact_store"):
            where.append("rating >= %(min_rating)s")
            params["min_rating"] = sc.min_rating
        if sc.attrs_filter:
            for k, v in sc.attrs_filter.items():
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", k):
                    raise CompileError(f"attrs 键名非法: {k!r}")
                pname = f"attr_{k}"
                where.append(f"attrs->'$.{k}' = %({pname})s")
                params[pname] = v if isinstance(v, (str, int, float)) else str(v)
        if spec.spatial.time_bucket and spec.target == "fact_traffic_grid":
            where.append("time_bucket = %(time_bucket)s")
            params["time_bucket"] = spec.spatial.time_bucket

        # ---- 空间（两段式：MBR 预筛 + ST 精确复核） ----
        sp = spec.spatial
        if sp.center is not None and sp.radius_m is not None and spec.target in ("dim_poi", "fact_store"):
            lon, lat = sp.center
            r_deg = sp.radius_m / 111_320.0  # 米 → 度（本机实测口径）
            params.update(
                center_wkt=f"POINT({lon:.6f} {lat:.6f})",
                r_deg=round(r_deg, 6),
                x0=round(lon - r_deg, 6), x1=round(lon + r_deg, 6),
                y0=round(lat - r_deg, 6), y1=round(lat + r_deg, 6),
            )
            # ★ MBR 预筛：走 KEY(lon,lat)，这是压测后唯一有效的空间加速路径
            where.append("lon BETWEEN %(x0)s AND %(x1)s")
            where.append("lat BETWEEN %(y0)s AND %(y1)s")
            # ★ 精确复核用 _ST_Dwithin（OB 私有，单位：度）——这是
            #   docs/压测报告-10万级.md 与架构文档 §6.6 验证过的谓词形式，
            #   保持与已验证 SQL 一致，勿自行替换。
            #
            #   ⚠️ 实测备忘（2026-10-15 18:00，勿再犯我犯过的错）：
            #   驱动索引会在 idx_poi_bbox 与 idx_poi_cat_stat_rating 之间**随统计
            #   信息演化切换**——同一条 SQL 在 15:40 为 bbox/17324us，18:00 变为
            #   cat_stat_rating/43173us，与谓词写法、参数化、列数均无关（已做单因素
            #   实验排除）。因此：
            #     · EXPLAIN 断言只要求「走索引路径」，**不断言具体驱动索引**；
            #     · 不能把某次观察到的驱动索引当成不变量写进文档或断言；
            #     · 四模混合时若优化器选向量驱动，MBR 收益会被预过滤覆盖
            #       （当前实测含/不含 MBR 为 57.0/55.8 ms，差异在噪声内）；
            #       MBR 的确定性收益在纯空间查询（当前实测 4.7 vs 16.9 ms）。
            where.append("_ST_Dwithin(location, ST_GeomFromText(%(center_wkt)s), %(r_deg)s)")
            has_spatial = True
            select_items.append(_SPHERE_SELECT[spec.target])

        if sp.polygon_wkt and spec.target in ("dim_poi", "fact_store"):
            if not sp.polygon_wkt.upper().startswith("POLYGON"):
                raise CompileError("polygon_wkt 只支持 POLYGON")
            params["poly_wkt"] = sp.polygon_wkt
            # MBR 由 WKT 顶点求 min/max（边界计算，非空间裁决）
            xs, ys = _wkt_bbox(sp.polygon_wkt)
            params.update(x0=round(xs[0], 6), x1=round(xs[1], 6),
                          y0=round(ys[0], 6), y1=round(ys[1], 6))
            where.append("lon BETWEEN %(x0)s AND %(x1)s")
            where.append("lat BETWEEN %(y0)s AND %(y1)s")
            where.append("ST_Contains(ST_GeomFromText(%(poly_wkt)s), location)")
            has_spatial = True

        # ---- 全文（AND 语义） ----
        ft_col = _FT_COLUMN[spec.target]
        if spec.semantic.keywords:
            match_expr = _build_match_expr(spec.semantic.keywords)
            params["match_expr"] = match_expr
            where.append(f"MATCH({ft_col}) AGAINST (%(match_expr)s IN BOOLEAN MODE)")

        # ---- SELECT 基础列（语义距离列由下方按需追加） ----
        if spec.target == "dim_poi":
            select_items = ["poi_id", "name", "category_id", "rating"] + select_items
        elif spec.target == "fact_store":
            select_items = ["store_id", "name", "rent", "area_sqm", "streetfront_tier"] + select_items
        else:  # knowledge_doc
            select_items = ["doc_id", "title"]

        if has_semantic:
            select_items.append(_VEC_DIST_SELECT[spec.target])

        params["limit"] = spec.semantic.limit
        params["qvec"] = qvec

        # 期望算子：语义目标走向量索引；否则走范围扫描
        expected: tuple[str, ...]
        if has_semantic:
            expected = ("VECTOR INDEX ADAPTIVE SCAN", "VECTOR INDEX SCAN")
        elif has_spatial:
            expected = ("TABLE RANGE SCAN",)
        else:
            expected = ("TABLE RANGE SCAN", "TABLE FULL SCAN")

        return {
            "where": where, "params": params, "select_items": select_items,
            "has_spatial": has_spatial, "has_semantic": has_semantic,
            "expected": expected, "limit": spec.semantic.limit, "qvec": qvec, "target": spec.target,
        }

    # ------------------------------------------------------------------
    def compile_hybrid(self, spec: HybridQuerySpec, qvec: str | None = None) -> CompiledQuery:
        """编译四类约束为一条 SQL（向量驱动，ADAPTIVE SCAN 自适应预过滤）。"""
        p = self._compile_parts(spec, qvec)
        sql = "SELECT " + ", ".join(p["select_items"]) + f"\nFROM {p['target']}"
        if p["where"]:
            sql += "\nWHERE " + "\n  AND ".join(p["where"])
        if p["has_semantic"]:
            sql += f"\nORDER BY {_VEC_ORDER[p['target']]} APPROXIMATE"
        sql += "\nLIMIT %(limit)s"
        return self._wrap(sql, p)

    # ------------------------------------------------------------------
    def compile_two_stage(self, spec: HybridQuerySpec, qvec: str | None = None) -> CompiledQuery:
        """写法 B 两阶段兜底（架构 §6.6）：先物化候选，再库内精确向量重排。

        用于 EXPLAIN 断言失败时的降级：第一阶段把空间/标量/全文候选（含 emb）
        物化成小结果集，第二阶段在库内做**精确** cosine 排序（不用 APPROXIMATE）。
        """
        p = self._compile_parts(spec, qvec)
        if not p["has_semantic"]:
            raise CompileError("两阶段写法需要语义条件")
        params = dict(p["params"])
        stage1_limit = max(p["limit"] * 20, 200)
        params["stage1_limit"] = stage1_limit
        where_block = "\n  AND ".join(p["where"])
        sql = (
            "WITH cand AS (\n"
            "  SELECT poi_id, name, category_id, rating, location, emb\n"
            f"  FROM {p['target']}\n"
            f"  WHERE {where_block}\n"
            "  LIMIT %(stage1_limit)s\n"
            ")\n"
            "SELECT poi_id, name, category_id, rating,\n"
            "       ROUND(ST_Distance_Sphere(location, ST_GeomFromText(%(center_wkt)s)),0) AS dist_m,\n"
            "       ROUND(cosine_distance(emb, %(qvec)s),4) AS sem_dist\n"
            "FROM cand\n"
            "ORDER BY cosine_distance(emb, %(qvec)s)\n"
            "LIMIT %(limit)s"
        )
        return self._wrap(sql, p)

    # ------------------------------------------------------------------
    def _wrap(self, sql: str, p: dict[str, Any]) -> CompiledQuery:
        return CompiledQuery(
            sql=sql, params=p["params"], target=p["target"],
            columns=_extract_columns(sql, p["target"]), tables=(p["target"],),
            has_spatial=p["has_spatial"], has_semantic=p["has_semantic"],
            expected_operators=p["expected"],
        )

    # ------------------------------------------------------------------
    # 卡点 1：静态校验
    # ------------------------------------------------------------------
    def validate(self, cq: CompiledQuery) -> None:
        """静态校验。失败抛 SQLValidationError（带 code）。

        注意：列校验**从 SQL 文本重新提取**（不信任 cq.columns）——
        否则调用方篡改 SQL 后 columns 还是旧值，跨表串列抓不住（实测踩坑）。
        """
        body = cq.sql.strip().rstrip(";")
        if ";" in body:
            raise SQLValidationError("MULTI_STATEMENT", "SQL 含分号，疑似多语句")
        if _FORBIDDEN_SQL.search(body):
            raise SQLValidationError("NOT_READONLY", "SQL 含写操作关键字")
        if _SELECT_STAR.search(body):
            raise SQLValidationError("SELECT_STAR", "禁止 SELECT *")
        for t in cq.tables:
            if t not in _READONLY_TABLES:
                raise SQLValidationError("UNKNOWN_TABLE", f"表不在白名单: {t}")
        extracted = _extract_columns(cq.sql, cq.tables)   # 重新提取，防篡改
        if set(extracted) != set(cq.columns):
            raise SQLValidationError(
                "COLUMN_SET_CHANGED",
                f"列集合与编译产物不一致: {sorted(set(extracted) ^ set(cq.columns))}")
        if cq.has_spatial and "BETWEEN" not in body.upper():
            # 编译阶段已强制，这里是对产物的兜底断言
            raise SQLValidationError("SPATIAL_WITHOUT_MBR", "空间谓词缺 MBR 预筛（两段式被破坏）")
        if cq.has_semantic:
            if "APPROXIMATE" not in body.upper():
                raise SQLValidationError("ANN_WITHOUT_APPROXIMATE", "向量排序缺 APPROXIMATE")
            if "MATCH" in body.upper() and "AGAINST" not in body.upper():
                raise SQLValidationError("BAD_MATCH", "MATCH 缺 AGAINST")
        if cq.params.get("limit", 0) > self.s.max_limit:
            raise SQLValidationError("LIMIT_TOO_LARGE", f"LIMIT 超过 {self.s.max_limit}")
        if "match_expr" in cq.params and not _KW_OK.search(
                cq.params["match_expr"].replace("+", "").replace(" ", "")):
            raise SQLValidationError("BAD_MATCH_EXPR", "全文表达式含白名单外字符")

    # ------------------------------------------------------------------
    # 卡点 2：dry-run
    # ------------------------------------------------------------------
    def dry_run(self, cq: CompiledQuery) -> None:
        wrapped = f"SELECT * FROM ({cq.sql.rstrip().rstrip(';')}) AS _dr LIMIT 1"
        try:
            self.ob.execute(wrapped, cq.params)
        except Exception as exc:  # OBError 或驱动层异常统一收敛
            raise DryRunError(f"dry-run failed: {exc}") from exc

    # ------------------------------------------------------------------
    # 卡点 3：EXPLAIN 算子断言
    # ------------------------------------------------------------------
    def assert_plan(self, cq: CompiledQuery) -> str:
        """断言计划走了索引路径，返回计划全文。

        只断言「出现期望算子」（即没有退化成 TABLE FULL SCAN），
        **不断言具体驱动索引**——驱动索引随统计信息演化切换（见 compile 内备忘）。
        """
        plan = self.ob.explain(cq.sql, cq.params)
        hit = [op for op in cq.expected_operators if op in plan]
        if not hit:
            raise PlanAssertionError(cq.expected_operators, plan)
        return plan

    # ------------------------------------------------------------------
    # 网格聚合（spatial_join_agg）：grid_id 冗余列 JOIN，不用 ST_Contains
    # ------------------------------------------------------------------
    def compile_grid_agg(self, spec: GridAggSpec) -> CompiledQuery:
        """客流网格 × POI 密度聚合。

        压测结论：ST_Contains 连接全扫（当前实测 274 ms），grid_id 冗余列 JOIN
        走 MERGE JOIN + TABLE RANGE SCAN（当前实测 2.2 ms），且结果逐行一致。
        """
        grid_ids = spec.grid_ids
        if not grid_ids:
            if spec.center is None or spec.radius_m is None:
                raise CompileError("grid_ids 与 center+radius_m 必须给其一")
            grid_ids = self.s.grid_ids_in_radius(*spec.center, spec.radius_m)
            if not grid_ids:
                raise CompileError("center+radius_m 未覆盖任何网格")
        grid_ids = sorted({int(g) for g in grid_ids})
        if len(grid_ids) > 500:
            raise CompileError(f"grid_ids 过多: {len(grid_ids)}（上限 500）")

        params: dict[str, Any] = {"time_bucket": spec.time_bucket, "limit": spec.limit}
        for i, g in enumerate(grid_ids):
            params[f"g{i}"] = g
        in_clause = ", ".join(f"%({f'g{i}'})s" for i in range(len(grid_ids)))

        where = ["g.time_bucket = %(time_bucket)s", f"g.grid_id IN ({in_clause})"]
        join_extra = ""
        if spec.category_id is not None:
            params["category_id"] = spec.category_id
            join_extra = " AND p.category_id = %(category_id)s"

        sql = (
            "SELECT g.grid_id, SUM(g.flow_cnt) AS flow, COUNT(p.poi_id) AS poi_cnt, "
            "ROUND(AVG(p.rating),2) AS avg_rating\n"
            "FROM fact_traffic_grid g LEFT JOIN dim_poi p "
            f"ON p.grid_id = g.grid_id{join_extra}\n"
            "WHERE " + "\n  AND ".join(where) + "\n"
            "GROUP BY g.grid_id ORDER BY flow DESC LIMIT %(limit)s"
        )
        return CompiledQuery(
            sql=sql, params=params, target="fact_traffic_grid",
            columns=_extract_columns(sql, ("fact_traffic_grid", "dim_poi")),
            tables=("fact_traffic_grid", "dim_poi"),
            expected_operators=("MERGE JOIN", "NESTED-LOOP JOIN", "TABLE RANGE SCAN"),
        )

    # ------------------------------------------------------------------
    # 客流聚合（traffic_grid_agg）：纯网格表查询
    # ------------------------------------------------------------------
    def compile_traffic_agg(self, spec: GridAggSpec) -> CompiledQuery:
        grid_ids = spec.grid_ids
        if not grid_ids:
            if spec.center is None or spec.radius_m is None:
                raise CompileError("grid_ids 与 center+radius_m 必须给其一")
            grid_ids = self.s.grid_ids_in_radius(*spec.center, spec.radius_m)
        grid_ids = sorted({int(g) for g in grid_ids}) if grid_ids else []

        params: dict[str, Any] = {"time_bucket": spec.time_bucket, "limit": spec.limit}
        where = ["time_bucket = %(time_bucket)s"]
        if grid_ids:
            if len(grid_ids) > 500:
                raise CompileError(f"grid_ids 过多: {len(grid_ids)}（上限 500）")
            for i, g in enumerate(grid_ids):
                params[f"g{i}"] = g
            in_clause = ", ".join(f"%({f'g{i}'})s" for i in range(len(grid_ids)))
            where.append(f"grid_id IN ({in_clause})")

        sql = ("SELECT grid_id, time_bucket, resident_cnt, flow_cnt\n"
               "FROM fact_traffic_grid\nWHERE " + "\n  AND ".join(where) + "\n"
               "ORDER BY flow_cnt DESC LIMIT %(limit)s")
        return CompiledQuery(
            sql=sql, params=params, target="fact_traffic_grid",
            columns=_extract_columns(sql, "fact_traffic_grid"),
            tables=("fact_traffic_grid",),
            expected_operators=("TABLE RANGE SCAN",),
        )

    # ------------------------------------------------------------------
    # 点查（poi_detail）
    # ------------------------------------------------------------------
    def compile_poi_detail(self, spec: PoiDetailSpec) -> CompiledQuery:
        sql = ("SELECT poi_id, name, category_id, brand, address, city, rating, attrs, status, "
               "ROUND(ST_Distance_Sphere(location, ST_GeomFromText(%(center_wkt)s)),0) AS dist_m\n"
               "FROM dim_poi WHERE poi_id = %(poi_id)s LIMIT 1")
        params: dict[str, Any] = {"poi_id": spec.poi_id, "center_wkt": "POINT(0 0)"}
        return CompiledQuery(
            sql=sql, params=params, target="dim_poi",
            columns=_extract_columns(sql, "dim_poi"), tables=("dim_poi",),
            expected_operators=("TABLE GET", "TABLE RANGE SCAN"),
        )

    # ------------------------------------------------------------------
    # 名称点查（geocode 库内兜底）：§5.6 只扩展——新增模板，既有编译/校验管线一行未改
    # ------------------------------------------------------------------
    def compile_poi_by_name(self, spec: PoiNameSpec) -> CompiledQuery:
        """dim_poi 按名称模糊点查（含 lon/lat，这是 compile_hybrid 的 SELECT 项没有的）。

        name 无索引 → 期望算子如实写「RANGE SCAN 或 FULL SCAN」，不假装索引命中；
        2 万行实测全表扫在毫秒级（docs/压测报告-10万级.md 同口径）。
        """
        like = re.sub(r"([%_\\])", r"\\\1", spec.name.strip())
        params: dict[str, Any] = {"status": 1, "name_like": f"%{like}%", "limit": spec.limit}
        where = ["status = %(status)s", "name LIKE %(name_like)s"]
        if spec.city:
            params["city"] = spec.city
            where.append("city = %(city)s")
        sql = ("SELECT poi_id, name, address, city, lon, lat, rating\nFROM dim_poi\nWHERE "
               + "\n  AND ".join(where) + "\nORDER BY rating DESC, poi_id ASC LIMIT %(limit)s")
        return CompiledQuery(
            sql=sql, params=params, target="dim_poi",
            columns=_extract_columns(sql, "dim_poi"), tables=("dim_poi",),
            expected_operators=("TABLE RANGE SCAN", "TABLE FULL SCAN"),
        )

    # ------------------------------------------------------------------
    # 全流程
    # ------------------------------------------------------------------
    def build_and_check(self, spec: HybridQuerySpec, qvec: str | None = None) -> tuple[CompiledQuery, str]:
        """编译 → 校验 → dry-run → EXPLAIN 断言。返回 (产物, 计划文本)。"""
        cq = self.compile_hybrid(spec, qvec)
        self.validate(cq)
        self.dry_run(cq)
        plan = self.assert_plan(cq)
        return cq, plan

    # ------------------------------------------------------------------
    # 主键批量点查（§9.3 图层端点取坐标）：同样是「只扩展、不改管线」
    # ------------------------------------------------------------------
    def compile_point_query(self, spec: PointQuerySpec) -> CompiledQuery:
        """按主键取坐标列。dim_poi 走 poi_id(PK)，网格走 (grid_id, time_bucket) 复合 PK。

        列名**不做别名**（网格表就是 center_lon/center_lat）：把别名写成 lon/lat 会被
        `_extract_columns` 判成「lon/lat 不属于 fact_traffic_grid」→ 跨表串列拦截。
        口径统一交给调用方映射，校验层保持表的真实列面。
        """
        if spec.time_bucket and spec.table != "fact_traffic_grid":
            raise CompileError(f"time_bucket 只适用于 fact_traffic_grid，当前表 {spec.table}")
        id_col, lon_col, lat_col = _POINT_COLUMNS[spec.table]
        ids = sorted({int(i) for i in spec.ids})
        if len(ids) > self.s.max_limit:
            raise CompileError(f"ids 过多: {len(ids)}（上限 max_limit={self.s.max_limit}）")

        params: dict[str, Any] = {"limit": len(ids)}
        for i, pk in enumerate(ids):
            params[f"id{i}"] = pk
        where = [f"{id_col} IN (" + ", ".join(f"%(id{i})s" for i in range(len(ids))) + ")"]
        if spec.time_bucket:
            params["time_bucket"] = spec.time_bucket
            where.append("time_bucket = %(time_bucket)s")

        sql = (f"SELECT {id_col}, {lon_col}, {lat_col}\nFROM {spec.table}\nWHERE "
               + "\n  AND ".join(where) + "\nLIMIT %(limit)s")
        return CompiledQuery(
            sql=sql, params=params, target=spec.table,
            columns=_extract_columns(sql, spec.table), tables=(spec.table,),
            expected_operators=("TABLE GET", "TABLE RANGE SCAN"),
        )

    # ------------------------------------------------------------------
    # 三层卡点全过再执行（供 L1 端点等「非 Skill」调用方复用同一套卡点）
    # ------------------------------------------------------------------
    def execute_checked(self, cq: CompiledQuery) -> tuple[list[dict[str, Any]], str]:
        """validate → dry_run → assert_plan → 执行；返回 (行集, EXPLAIN 计划)。"""
        self.validate(cq)
        self.dry_run(cq)
        plan = self.assert_plan(cq)
        rows, _ms = self.ob.execute_timed(cq.sql, cq.params)
        return rows, plan


def _wkt_bbox(wkt: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """从 POLYGON WKT 顶点求 (lon_min,max) 与 (lat_min,max)。

    仅做边界包围盒计算（min/max），不做任何几何裁决 —— 空间裁决全部下推 OB。
    """
    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", wkt)]
    if len(nums) < 6 or len(nums) % 2 != 0:
        raise CompileError(f"WKT 顶点解析失败: {wkt[:60]}")
    xs = nums[0::2]
    ys = nums[1::2]
    return (min(xs), max(xs)), (min(ys), max(ys))


def _build_match_expr(keywords: list[str]) -> str:
    """关键词 → ngram 布尔 AND 表达式（'+kw1 +kw2'）。

    放行条件：每个词必须通过 _KW_OK 字符白名单。这是本模块唯一允许的
    "受控拼接"——用户输入不直接进 SQL，而是经白名单过滤后构造布尔表达式，
    从根本上排除引号/注释/分号注入。
    """
    cleaned = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        if not _KW_OK.match(kw):
            raise CompileError(f"关键词含非法字符: {kw!r}")
        cleaned.append("+" + kw)
    if not cleaned:
        raise CompileError("keywords 全部为空")
    return " ".join(cleaned)
