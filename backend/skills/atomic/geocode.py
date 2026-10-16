"""原子 Skill：地理编码（架构 §2.2 L3「地理编码」/ §7 表「地址→坐标：高德 API，TTL 7 天」）。

铁律 2：run() 只调 SpatialRouter——不拼 SQL、不算坐标。
高德在线 / 意图词表 / dim_poi 三级兜底与 GCJ-02→WGS84 归一全在路由层
（backend/services/spatial_router.py，架构 §2.2 L4）。
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from backend.services.spatial_router import RouteUnavailable
from backend.skills.base import SkillContext, SkillMetadata, SpatialSkill, elapsed_ms_since
from backend.skills.registry import register_skill

# source → 可解释证据（Evidence.type ∈ {sql, data, amap, llm}，架构 §4.1）
_EVIDENCE: dict[str, dict[str, str]] = {
    "amap": {"type": "amap", "ref": "geocode", "note": "高德在线地理编码（GCJ-02→WGS84 归一）"},
    "place_dict": {"type": "data", "ref": "PLACE_COORDS", "note": "意图地名词表兜底（离线 WGS84）"},
    "dim_poi": {"type": "sql", "ref": "compile_poi_by_name", "note": "库内兜底：dim_poi name LIKE 三层卡点"},
    "input": {"type": "data", "ref": "caller", "note": "调用方直给 WGS84 坐标，未做定位"},
    "none": {"type": "data", "ref": "geocode", "note": "三级兜底全未命中，不猜坐标"},
}


class GeocodeInput(BaseModel):
    address: str = Field(min_length=1, max_length=60, description="地名或地址")
    city: str = Field(default="", max_length=30, description="城市限定（高德 city / dim_poi.city）")


class PlaceOut(BaseModel):
    """定位结果归一化形态（path_planning / reachability 复用，避免三处漂移）。"""

    name: str
    lon: float = Field(description="WGS84 经度")
    lat: float = Field(description="WGS84 纬度")
    source: str = Field(description="amap | place_dict | dim_poi | input")


class GeocodeOutput(BaseModel):
    found: bool
    name: str = ""
    lon: float | None = Field(default=None, description="WGS84（库内同坐标系）")
    lat: float | None = None
    gcj02: tuple[float, float] | None = Field(default=None, description="GCJ-02（高德底图，渲染用）")
    address: str = ""
    poi_id: int | None = None
    source: str = "none"
    degraded: bool = Field(default=True, description="True = 未走高德在线（词表或库内兜底）")
    reason: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: float = 0.0


@register_skill
class GeocodeSkill(SpatialSkill[GeocodeInput, GeocodeOutput]):
    """地名/地址 → 坐标：高德在线优先，无 key 时词表 → dim_poi 兜底并如实打标。"""

    metadata: SkillMetadata
    input_schema = GeocodeInput
    output_schema = GeocodeOutput

    def __init__(self) -> None:
        self.metadata = SkillMetadata(
            name="geocode",
            version="1.0.0",
            scene="atomic",
            description="地理编码：地名/地址→坐标（WGS84 出口，GCJ-02 附带）；高德在线优先，无 key 走词表/库内兜底",
            intent_examples=["春熙路在哪里", "天府广场的坐标", "把地址转成经纬度"],
        )

    def build_plan(self, ctx: SkillContext) -> list[dict]:
        return [{"id": self.metadata.name, "skill": self.metadata.name}]

    def run(self, inputs: GeocodeInput, ctx: SkillContext) -> GeocodeOutput:
        router = ctx.services.router
        t0 = time.perf_counter()
        try:
            p = router.geocode(inputs.address, inputs.city)
        except RouteUnavailable as exc:
            # 兜底链走完仍无结果：返回诚实的空结果（不把高德专有异常抛给 L2 图）
            return GeocodeOutput(
                found=False, source="none", degraded=True, reason=str(exc),
                evidence=[_EVIDENCE["none"]], elapsed_ms=elapsed_ms_since(t0),
            )
        return GeocodeOutput(
            found=True, name=p.name, lon=p.lon, lat=p.lat, gcj02=p.gcj02,
            address=p.address, poi_id=p.poi_id, source=p.source,
            degraded=p.source != "amap", evidence=[_EVIDENCE.get(p.source, _EVIDENCE["none"])],
            elapsed_ms=elapsed_ms_since(t0),
        )


__all__ = ["GeocodeInput", "GeocodeOutput", "GeocodeSkill", "PlaceOut"]
