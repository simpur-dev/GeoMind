"""场景 Skill：智能选址（架构 §5.1 的样板场景）。

只写「编排 + 评分口径 + 渲染模板」，数据访问全在 L4（QueryBuilder）。
DAG：multi_modal_search → spatial_join_agg → score_and_rank → render_spec。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from backend.config import Settings, get_settings
from backend.skills.base import SkillContext, SkillMetadata, SpatialSkill
from backend.skills.registry import register_skill


class SiteSelectionInput(BaseModel):
    """智能选址入参（场景 Skill 扁平化四类约束）。"""

    center: tuple[float, float]
    radius_m: float = Field(default=1500, gt=0, le=100_000)
    category_id: int | None = None
    keywords: list[str] = Field(default_factory=list)
    query_text: str | None = None
    min_rating: float | None = Field(default=4.0, ge=0, le=5)
    time_bucket: str = "workday_evening"
    limit: int = Field(default=10, ge=1, le=100)


class ScoredCandidate(BaseModel):
    grid_id: int
    flow: int | None = None
    poi_cnt: int = 0
    avg_rating: float | None = None
    score: float = 0.0
    reason: str = ""


class SiteSelectionOutput(BaseModel):
    scene: str = "site_selection"
    candidates: list[ScoredCandidate] = Field(default_factory=list)
    summary: str = ""
    layers: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: float = 0.0


@register_skill
class SiteSelectionSkill(SpatialSkill[SiteSelectionInput, SiteSelectionOutput]):
    """智能选址：多模检索候选 → 网格聚合 → 加权打分 → Map Spec。"""

    metadata: SkillMetadata
    input_schema = SiteSelectionInput
    output_schema = SiteSelectionOutput
    required_tools = ["multi_modal_search", "spatial_join_agg"]

    def __init__(self) -> None:
        self.metadata = SkillMetadata(
            name="site_selection",
            version="1.0.0",
            scene="site_selection",
            description="智能选址：多模检索候选 → 网格客流聚合 → 三维加权打分 → Map Spec",
            intent_examples=["帮我在春熙路附近选个咖啡馆位置", "选址", "开在哪里合适"],
        )
        self.s: Settings = get_settings()

    # ------------------------------------------------------------------
    def build_plan(self, ctx: SkillContext) -> list[dict]:
        """可执行 DAG 模板（架构 §5.2：节点引用原子工具名）。"""
        return [
            {"id": "s1_candidates", "skill": "multi_modal_search"},
            {"id": "s2_spatial_join", "skill": "spatial_join_agg",
             "depends_on": ["s1_candidates"]},
            {"id": "s3_score", "skill": "score_and_rank", "depends_on": ["s2_spatial_join"]},
        ]

    # ------------------------------------------------------------------
    def run(self, inputs: SiteSelectionInput, ctx: SkillContext) -> SiteSelectionOutput:
        tools = ctx.tools

        # s1 多模检索（复用已验证的四模 SQL）
        from backend.skills.atomic.multi_modal_search import MultiModalSearchInput

        s1 = tools["multi_modal_search"].run(MultiModalSearchInput(
            center=inputs.center, radius_m=inputs.radius_m, category_id=inputs.category_id,
            keywords=inputs.keywords, query_text=inputs.query_text,
            min_rating=inputs.min_rating, limit=inputs.limit,
        ), ctx)

        # s2 网格聚合（grid_id 冗余列 JOIN）
        from backend.skills.atomic.spatial_join_agg import SpatialJoinAggInput

        s2 = tools["spatial_join_agg"].run(SpatialJoinAggInput(
            center=inputs.center, radius_m=inputs.radius_m,
            time_bucket=inputs.time_bucket, category_id=inputs.category_id, limit=100,
        ), ctx)

        # s3 打分（纯 Python 对 SQL 返回的小结果集计算，不写进 ANN SQL）
        cands = self._score(s1, s2)
        summary = self.synthesize_summary(cands)
        layers = self.render_spec(ctx, ctx.session_id)["layers"] if cands else []
        evidence = [
            {"type": "sql", "ref": "multi_modal_search", "note": "四模混合检索"},
            {"type": "sql", "ref": "spatial_join_agg", "note": "网格聚合（grid_id JOIN）"},
        ]
        return SiteSelectionOutput(
            candidates=cands, summary=summary, layers=layers, evidence=evidence,
            elapsed_ms=round(s1.elapsed_ms + s2.elapsed_ms, 2),
        )

    # ------------------------------------------------------------------
    def _score(self, s1: Any, s2: Any) -> list[ScoredCandidate]:
        """三维加权打分：客流 + 评分 - 竞品密度（权重见 config，可解释可复现）。"""
        w_sem, w_traffic, w_rating, w_comp = (
            self.s.score_weight_semantic, self.s.score_weight_traffic,
            self.s.score_weight_rating, self.s.score_weight_competition)
        max_flow = max((a.flow or 0) for a in s2.aggs) or 1
        max_poi = max((a.poi_cnt for a in s2.aggs), default=1) or 1
        out: list[ScoredCandidate] = []
        for a in s2.aggs:
            flow_n = (a.flow or 0) / max_flow
            rating_n = float(a.avg_rating or 0) / 5.0
            comp_n = a.poi_cnt / max_poi
            score = w_traffic * flow_n + w_rating * rating_n - w_comp * comp_n
            out.append(ScoredCandidate(
                grid_id=a.grid_id, flow=a.flow, poi_cnt=a.poi_cnt, avg_rating=float(a.avg_rating or 0),
                score=round(score, 4),
                reason=f"客流归一 {flow_n:.2f}×{w_traffic} + 评分 {rating_n:.2f}×{w_rating} "
                       f"- 竞品密度 {comp_n:.2f}×{w_comp}",
            ))
        out.sort(key=lambda c: c.score, reverse=True)
        _ = w_sem  # 语义权重用于候选层排序解释，保留配置位
        return out[:10]

    def synthesize_summary(self, cands: list[ScoredCandidate]) -> str:
        if not cands:
            return "该范围内未找到满足条件的网格。"
        top = cands[0]
        return (f"推荐网格 {top.grid_id}：客流 {top.flow}、咖啡类 POI {top.poi_cnt} 个、"
                f"平均评分 {top.avg_rating}，综合得分 {top.score}（权重：客流 "
                f"{self.s.score_weight_traffic} / 评分 {self.s.score_weight_rating} / "
                f"竞品密度 {self.s.score_weight_competition}）。")

    # ------------------------------------------------------------------
    def render_spec(self, ctx: SkillContext, session_id: str = "",
                    state: dict[str, Any] | None = None) -> dict[str, Any]:
        """Map Spec（架构 §9.3 完整形态，经 contracts 模型校验后返回）。

        坐标系统一为 GCJ-02（高德底图）；WGS84 → GCJ-02 转换在 geo 模块。
        layers 的 source.href 指向后端 GeoJSON 端点，前端只渲染不计算。
        """
        from backend.api.contracts.map_spec import (
            LayerSource,
            LayerSpec,
            MapSpec,
            MapView,
            PanelSpec,
        )
        from backend.api.geo import viewport_from_center

        st = state or {}
        spec = st.get("spec") or {}
        center = tuple(spec["center"]) if spec.get("center") else (104.0817, 30.657)
        radius_m = float(spec.get("radius_m") or 1500)

        href = f"/api/sessions/{session_id}/layers"
        layers = [
            LayerSpec(id="l_candidates", kind="poi",
                      source=LayerSource(href=f"{href}/l_candidates", type="geojson"),
                      data_ref="candidates",
                      style={"radius": 6, "color": "#1677FF", "labelBy": "name"},
                      interactions=["click", "hover"], status="ready",
                      derived_from=["s1_candidates"]),
            LayerSpec(id="l_heat", kind="heatmap",
                      source=LayerSource(href=f"{href}/l_heat", type="geojson"),
                      data_ref="fact_traffic_grid",
                      style={"radius": 24, "opacity": 0.6, "valueKey": "flow"},
                      interactions=[], status="ready",
                      derived_from=["s2_spatial_join"]),
        ]
        panels = [
            PanelSpec(id="p_ranking", kind="ranking", data_ref="candidates", title="候选排名"),
            PanelSpec(id="p_metrics", kind="metrics", data_ref="decision.metrics", title="关键指标"),
        ]
        map_spec = MapSpec(version="1.0", map_view=MapView(**viewport_from_center(center, radius_m)),
                           layers=layers, panels=panels)
        return map_spec.model_dump(mode="json")
