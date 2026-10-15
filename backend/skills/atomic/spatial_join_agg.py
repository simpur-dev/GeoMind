"""原子 Skill：网格聚合（客流 × POI 密度）。

压测结论固化：用 grid_id 冗余列 JOIN（MERGE JOIN + TABLE RANGE SCAN），
不用 ST_Contains 连接（全扫）。两种写法结果已实测逐行一致。
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from backend.query_builder import GridAggSpec
from backend.skills.base import SkillContext, SkillMetadata, SpatialSkill
from backend.skills.registry import register_skill


class SpatialJoinAggInput(BaseModel):
    grid_ids: list[int] | None = None
    center: tuple[float, float] | None = None
    radius_m: float | None = Field(default=None, gt=0, le=100_000)
    time_bucket: str | None = "workday_evening"
    category_id: int | None = None
    limit: int = Field(default=50, ge=1, le=500)


class GridAgg(BaseModel):
    grid_id: int
    flow: int | None = None
    poi_cnt: int = 0
    avg_rating: Decimal | None = None


class SpatialJoinAggOutput(BaseModel):
    aggs: list[GridAgg] = Field(default_factory=list)
    plan: str = ""
    elapsed_ms: float = 0.0


@register_skill
class SpatialJoinAggSkill(SpatialSkill[SpatialJoinAggInput, SpatialJoinAggOutput]):
    """按网格聚合客流与 POI 密度（选址场景的空间抽象层）。"""

    metadata: SkillMetadata
    input_schema = SpatialJoinAggInput
    output_schema = SpatialJoinAggOutput

    def __init__(self) -> None:
        self.metadata = SkillMetadata(
            name="spatial_join_agg",
            version="1.0.0",
            scene="atomic",
            description="网格聚合：客流网格 × POI 密度（grid_id 冗余列 JOIN，不用 ST_Contains）",
            intent_examples=["这一带的客流和店铺密度", "网格聚合统计"],
        )

    def build_plan(self, ctx: SkillContext) -> list[dict]:
        return [{"id": self.metadata.name, "skill": self.metadata.name}]

    def run(self, inputs: SpatialJoinAggInput, ctx: SkillContext) -> SpatialJoinAggOutput:
        svc = ctx.services
        cq = svc.query_builder.compile_grid_agg(GridAggSpec(
            grid_ids=inputs.grid_ids, center=inputs.center, radius_m=inputs.radius_m,
            time_bucket=inputs.time_bucket, category_id=inputs.category_id, limit=inputs.limit,
        ))
        svc.query_builder.validate(cq)
        svc.query_builder.dry_run(cq)
        plan = svc.query_builder.assert_plan(cq)
        rows, ms = svc.ob.execute_timed(cq.sql, cq.params)
        aggs = [GridAgg(
            grid_id=int(r["grid_id"]), flow=int(r["flow"]) if r.get("flow") is not None else None,
            poi_cnt=int(r["poi_cnt"] or 0), avg_rating=r.get("avg_rating"),
        ) for r in rows]
        return SpatialJoinAggOutput(aggs=aggs, plan=plan, elapsed_ms=round(ms, 2))
