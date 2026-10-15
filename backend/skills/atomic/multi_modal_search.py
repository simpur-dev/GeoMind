"""原子 Skill：多模混合检索（架构 §5.1，L3 → L4 Query Builder 的唯一检索出口）。

把 QueryBuilder 的四类约束编译 + 三层卡点 + 执行 + 结果归一封装成一个工具；
EXPLAIN 断言失败时自动降级到架构 §6.6 的「写法 B 两阶段」兜底，并置 degraded=True。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from backend.query_builder import (
    CompiledQuery,
    HybridQuerySpec,
    PlanAssertionError,
    QueryBuilder,
    ScalarConstraint,
    SemanticConstraint,
    SpatialConstraint,
)
from backend.skills.base import SkillContext, SkillMetadata, SpatialSkill
from backend.skills.registry import register_skill


# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------
class MultiModalSearchInput(BaseModel):
    """四类约束的扁平入参（自动导出 MCP JSON Schema）。"""

    center: tuple[float, float] | None = Field(default=None, description="(lon, lat) WGS84")
    radius_m: float | None = Field(default=None, gt=0, le=100_000)
    polygon_wkt: str | None = Field(default=None, description="POLYGON WKT，SRID 0")
    category_id: int | None = None
    min_rating: float | None = Field(default=None, ge=0, le=5)
    status: int = Field(default=1, ge=0, le=2)
    attrs_filter: dict[str, Any] | None = Field(default=None, description="JSON 属性等值条件")
    keywords: list[str] = Field(default_factory=list, description="ngram 全文词，AND 语义")
    query_text: str | None = Field(default=None, description="语义查询文本（转向量后 ANN）")
    limit: int = Field(default=10, ge=1, le=200)


class PoiCandidate(BaseModel):
    poi_id: int
    name: str
    category_id: int | None = None
    rating: Decimal | None = None
    dist_m: float | None = None
    sem_dist: float | None = None


class MultiModalSearchOutput(BaseModel):
    candidates: list[PoiCandidate] = Field(default_factory=list)
    plan: str = ""
    elapsed_ms: float = 0.0
    degraded: bool = Field(default=False, description="True = EXPLAIN 断言失败，走了两阶段兜底")
    sql: str = ""


@register_skill
class MultiModalSearchSkill(SpatialSkill[MultiModalSearchInput, MultiModalSearchOutput]):
    """四模/五模混合检索：MBR 预筛 + ST 复核 + 标量 + 全文 + 向量 ANN。"""

    input_schema = MultiModalSearchInput
    output_schema = MultiModalSearchOutput

    def __init__(self) -> None:
        self.metadata = SkillMetadata(
            name="multi_modal_search",
            version="1.0.0",
            scene="atomic",
            description="多模混合检索：空间(MBR+ST) + 标量 + 全文(ngram) + 向量(cosine ANN)，一条 SQL 下推 SeekDB",
            intent_examples=["附近有什么咖啡馆", "安静能自习的咖啡", "春熙路 1.5 公里内评分 4.5 以上的火锅"],
        )

    # ------------------------------------------------------------------
    def build_plan(self, ctx: SkillContext) -> list[dict]:
        return [{"id": self.metadata.name, "skill": self.metadata.name}]

    # ------------------------------------------------------------------
    def run(self, inputs: MultiModalSearchInput, ctx: SkillContext) -> MultiModalSearchOutput:
        svc = ctx.services
        qb: QueryBuilder = svc.query_builder

        qvec = svc.embedding.embed_query(inputs.query_text) if inputs.query_text else None

        spec = HybridQuerySpec(
            spatial=SpatialConstraint(
                center=inputs.center, radius_m=inputs.radius_m, polygon_wkt=inputs.polygon_wkt),
            scalar=ScalarConstraint(
                category_id=inputs.category_id, min_rating=inputs.min_rating,
                status=inputs.status, attrs_filter=inputs.attrs_filter),
            semantic=SemanticConstraint(
                keywords=inputs.keywords, query_text=inputs.query_text, limit=inputs.limit),
        )

        try:
            cq, plan = qb.build_and_check(spec, qvec)
            return self._execute(cq, plan, svc, degraded=False)
        except PlanAssertionError as exc:
            # 写法 B 两阶段兜底（架构 §6.6）：先物化候选，再库内精确向量重排
            cq = qb.compile_two_stage(spec, qvec)
            plan = exc.actual_plan
            return self._execute(cq, plan, svc, degraded=True)

    # ------------------------------------------------------------------
    def _execute(self, cq: CompiledQuery, plan: str, svc: Any, *, degraded: bool) -> MultiModalSearchOutput:
        rows, ms = svc.ob.execute_timed(cq.sql, cq.params)
        cands = [PoiCandidate(
            poi_id=int(r["poi_id"]),
            name=str(r.get("name", "")),
            category_id=r.get("category_id"),
            rating=r.get("rating"),
            dist_m=float(r["dist_m"]) if r.get("dist_m") is not None else None,
            sem_dist=float(r["sem_dist"]) if r.get("sem_dist") is not None else None,
        ) for r in rows]
        return MultiModalSearchOutput(
            candidates=cands, plan=plan, elapsed_ms=round(ms, 2),
            degraded=degraded, sql=cq.sql,
        )
