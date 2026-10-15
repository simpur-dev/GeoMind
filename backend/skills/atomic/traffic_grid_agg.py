"""原子 Skill：客流网格聚合（纯网格表，按客流排序）。"""
from __future__ import annotations

from pydantic import BaseModel, Field

from backend.query_builder import GridAggSpec
from backend.skills.base import SkillContext, SkillMetadata, SpatialSkill
from backend.skills.registry import register_skill


class TrafficGridAggInput(BaseModel):
    grid_ids: list[int] | None = None
    center: tuple[float, float] | None = None
    radius_m: float | None = Field(default=None, gt=0, le=100_000)
    time_bucket: str | None = "workday_evening"
    limit: int = Field(default=50, ge=1, le=500)


class TrafficGrid(BaseModel):
    grid_id: int
    time_bucket: str
    resident_cnt: int | None = None
    flow_cnt: int | None = None


class TrafficGridAggOutput(BaseModel):
    grids: list[TrafficGrid] = Field(default_factory=list)
    plan: str = ""
    elapsed_ms: float = 0.0


@register_skill
class TrafficGridAggSkill(SpatialSkill[TrafficGridAggInput, TrafficGridAggOutput]):
    """百米网格客流/人口查询（time_bucket 过滤 + 按客流降序）。"""

    metadata: SkillMetadata
    input_schema = TrafficGridAggInput
    output_schema = TrafficGridAggOutput

    def __init__(self) -> None:
        self.metadata = SkillMetadata(
            name="traffic_grid_agg",
            version="1.0.0",
            scene="atomic",
            description="百米网格客流/人口聚合：time_bucket 过滤，按客流降序",
            intent_examples=["哪些网格客流最高", "工作日晚上哪里人多"],
        )

    def build_plan(self, ctx: SkillContext) -> list[dict]:
        return [{"id": self.metadata.name, "skill": self.metadata.name}]

    def run(self, inputs: TrafficGridAggInput, ctx: SkillContext) -> TrafficGridAggOutput:
        svc = ctx.services
        cq = svc.query_builder.compile_traffic_agg(GridAggSpec(
            grid_ids=inputs.grid_ids, center=inputs.center, radius_m=inputs.radius_m,
            time_bucket=inputs.time_bucket, limit=inputs.limit,
        ))
        svc.query_builder.validate(cq)
        svc.query_builder.dry_run(cq)
        plan = svc.query_builder.assert_plan(cq)
        rows, ms = svc.ob.execute_timed(cq.sql, cq.params)
        grids = [TrafficGrid(
            grid_id=int(r["grid_id"]), time_bucket=str(r["time_bucket"]),
            resident_cnt=r.get("resident_cnt"), flow_cnt=r.get("flow_cnt"),
        ) for r in rows]
        return TrafficGridAggOutput(grids=grids, plan=plan, elapsed_ms=round(ms, 2))
