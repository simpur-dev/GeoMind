"""前后端 Map Spec 契约模型（架构 §9.3 的 Pydantic 落地）。

这是**前端契约的权威定义**：后端 render_spec 的产出必须通过本模块校验，
前端 web/src/api/mapspec.ts 的 TS 接口与本模块逐字段对齐（由测试钉死）。

kind 注册表（架构 §9.3）：poi / heatmap / polygon / isochrone / route /
trajectory / grid / marker。新增 kind = 前端注册新渲染器，核心零改动。

坐标系：本模块所有坐标均为 **GCJ-02**（高德底图坐标系）。
WGS84 → GCJ-02 的转换在后端完成（见 backend/api/geo.py），前端零计算。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# 架构 §9.3 的图层 kind 注册表（渲染器按此注册，见 web/src/layers/registry.ts）
LayerKind = Literal["poi", "heatmap", "polygon", "isochrone", "route",
                    "trajectory", "grid", "marker"]

LayerStatus = Literal["ready", "stale", "error"]

PanelKind = Literal["ranking", "metrics", "text"]

# 兼容 1.0 版样例数据（seed_cd.py 早期灌入 scene_solution 的 points/line 命名）
_KIND_ALIAS = {"points": "poi", "line": "route"}


class MapView(BaseModel):
    """镜头（GCJ-02 经纬度）。"""

    center: tuple[float, float]
    zoom: int = Field(default=13, ge=3, le=20)
    pitch: float = Field(default=0.0, ge=0, le=80)


class LayerSource(BaseModel):
    """图层数据来源：优先 href（REST 拉取），其次 inline（小结果集内联）。"""

    href: str | None = None
    type: Literal["geojson", "inline"] = "geojson"


class LayerSpec(BaseModel):
    """一个声明式图层（架构 §9.3 layers[] 元素）。

    前端只渲染不计算：style 全部来自后端；interactions 声明该图层支持的
    手势（click/hover/draw），由前端回传为结构化 feedback。
    """

    id: str
    kind: LayerKind
    source: LayerSource | None = None
    data_ref: str | None = Field(default=None, description="内联数据引用名（source.type=inline 时用）")
    style: dict[str, Any] = Field(default_factory=dict)
    interactions: list[str] = Field(default_factory=list)
    status: LayerStatus = "ready"
    derived_from: list[str] = Field(default_factory=list)

    @field_validator("kind", mode="before")
    @classmethod
    def _alias(cls, v: Any) -> Any:
        return _KIND_ALIAS.get(v, v)


class PanelSpec(BaseModel):
    """右栏面板声明（架构 §9.3 panels[] 元素）。"""

    id: str
    kind: PanelKind
    data_ref: str
    title: str = ""


class MapSpec(BaseModel):
    """完整 Map Spec（架构 §9.3 顶层结构：map_view + layers + panels）。"""

    version: str = "1.0"
    map_view: MapView | None = None
    layers: list[LayerSpec] = Field(default_factory=list)
    panels: list[PanelSpec] = Field(default_factory=list)


class FeedbackEvent(BaseModel):
    """地图手势 / 约束回传（架构 §9.3 POST /feedback 载荷）。

    手势几何**原样上报**（前端不解释）：draw_polygon 的 WKT 为 GCJ-02，
    后端 L1 翻译层负责转 WGS84 再进约束（架构 §9.6 职责边界表）。
    """

    type: Literal["draw_polygon", "point_select", "range_drag",
                  "constraint_edit", "adopt", "reject"]
    payload: dict[str, Any] = Field(default_factory=dict)
