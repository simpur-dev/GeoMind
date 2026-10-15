"""原子 Skill：POI 点查（+ 逆地理兜底）。

逆地理编码走高德网关；未配置 key 时 AmapGateway 抛 AmapUnavailable，
本 Skill 降级返回库内 address（阶段 0 不产生任何外部调用）。
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from backend.query_builder import PoiDetailSpec
from backend.skills.base import SkillContext, SkillMetadata, SpatialSkill
from backend.skills.registry import register_skill


class PoiDetailInput(BaseModel):
    poi_id: int


class PoiDetailOutput(BaseModel):
    poi_id: int
    name: str | None = None
    category_id: int | None = None
    brand: str | None = None
    address: str | None = None
    city: str | None = None
    rating: Decimal | None = None
    status: int | None = None
    regeo_address: str | None = Field(default=None, description="高德逆地理结果（无 key 时为空）")


@register_skill
class PoiDetailSkill(SpatialSkill[PoiDetailInput, PoiDetailOutput]):
    """POI 点查 + 可选逆地理兜底。"""

    metadata: SkillMetadata
    input_schema = PoiDetailInput
    output_schema = PoiDetailOutput

    def __init__(self) -> None:
        self.metadata = SkillMetadata(
            name="poi_detail",
            version="1.0.0",
            scene="atomic",
            description="POI 点查详情；可选高德逆地理编码兜底（无 key 自动降级）",
            intent_examples=["这个店怎么样", "POI 详情"],
        )

    def build_plan(self, ctx: SkillContext) -> list[dict]:
        return [{"id": self.metadata.name, "skill": self.metadata.name}]

    def run(self, inputs: PoiDetailInput, ctx: SkillContext) -> PoiDetailOutput:
        svc = ctx.services
        cq = svc.query_builder.compile_poi_detail(PoiDetailSpec(poi_id=inputs.poi_id))
        rows = svc.ob.execute(cq.sql, cq.params)
        if not rows:
            return PoiDetailOutput(poi_id=inputs.poi_id)
        r = rows[0]
        out = PoiDetailOutput(
            poi_id=int(r["poi_id"]), name=r.get("name"), category_id=r.get("category_id"),
            brand=r.get("brand"), address=r.get("address"), city=r.get("city"),
            rating=r.get("rating"), status=r.get("status"),
        )
        # 逆地理兜底：无 key 时降级（不外部调用）
        if svc.amap is not None and r.get("location") is None:
            try:
                out.regeo_address = svc.amap.regeo(out.address or "")
            except Exception:
                out.regeo_address = None
        return out
