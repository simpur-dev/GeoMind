"""Skill 层测试（设计文档 §8 第 2 行：对拍 + DAG + Map Spec）。"""
from __future__ import annotations

import pytest

from backend.skills.atomic.knowledge_search import KnowledgeSearchInput
from backend.skills.atomic.multi_modal_search import MultiModalSearchInput
from backend.skills.atomic.spatial_join_agg import SpatialJoinAggInput
from backend.skills.registry import SkillRegistry
from backend.skills.scenes.site_selection import SiteSelectionInput


@pytest.fixture()
def reg() -> SkillRegistry:
    r = SkillRegistry.instance()
    r.discover()
    return r


class TestRegistry:
    def test_discover_finds_all(self, reg):
        names = set(reg.skills)
        assert {"multi_modal_search", "spatial_join_agg", "traffic_grid_agg",
                "knowledge_search", "poi_detail", "site_selection"} <= names
        assert set(reg.atomic()) == names - {"site_selection"}

    def test_duplicate_name_rejected(self, reg):
        from backend.skills.base import SkillMetadata, SpatialSkill

        class Dup(SpatialSkill):
            metadata = None
            input_schema = MultiModalSearchInput
            output_schema = MultiModalSearchInput

            def __init__(self):
                self.metadata = SkillMetadata(name="multi_modal_search", description="dup")

            def build_plan(self, ctx): return []

            def run(self, inputs, ctx): return None

        with pytest.raises(ValueError):
            reg.register(Dup())

    def test_describe_has_json_schema(self, reg):
        for d in reg.describe_all():
            assert d["inputSchema"]["type"] == "object"
            assert "properties" in d["inputSchema"]


class TestMultiModalSearch:
    def test_matches_handwritten_sql(self, services, skill_ctx, db_ok):
        """Skill 输出必须与手工 SQL 对拍一致（同一 top10 集合）。"""
        from backend.embeddings import SyntheticEmbeddingProvider
        from backend.etl.seed_cd import gen_query_vector  # noqa: F401

        prov = SyntheticEmbeddingProvider()
        qvec = prov.embed_query("安静可自习的咖啡馆")

        out = skill_ctx.tools["multi_modal_search"].run(MultiModalSearchInput(
            center=(104.0817, 30.6570), radius_m=1500, category_id=1, min_rating=4.0,
            keywords=["咖啡"], query_text="安静可自习的咖啡馆", limit=10,
        ), skill_ctx)
        assert out.candidates and not out.degraded
        skill_ids = [c.poi_id for c in out.candidates]

        # 手工 SQL（架构 §6.6 写法 A + 类目关键词）
        rows = services.ob.execute(
            "SELECT poi_id FROM dim_poi "
            "WHERE status=1 AND category_id=1 AND rating>=4.0 "
            "AND lon BETWEEN 104.0682 AND 104.0952 AND lat BETWEEN 30.6435 AND 30.6705 "
            "AND _ST_Dwithin(location, ST_GeomFromText('POINT(104.0817 30.6570)'), 0.0135) "
            "AND MATCH(name) AGAINST ('+咖啡' IN BOOLEAN MODE) "
            f"ORDER BY cosine_distance(emb,'{qvec}') APPROXIMATE LIMIT 10")
        manual_ids = [int(r["poi_id"]) for r in rows]
        assert skill_ids == manual_ids
        assert all(c.dist_m is not None and c.dist_m <= 1500 for c in out.candidates)

    def test_injection_rejected_end_to_end(self, skill_ctx):
        out_or_exc = None
        try:
            skill_ctx.tools["multi_modal_search"].run(MultiModalSearchInput(
                center=(104.0817, 30.6570), radius_m=1500,
                keywords=["x'; DROP TABLE dim_poi; --"], limit=5), skill_ctx)
        except Exception as exc:  # noqa: BLE001
            out_or_exc = exc
        assert out_or_exc is not None


class TestSpatialJoinAgg:
    def test_gridid_join_matches_st_contains(self, services, skill_ctx, db_ok):
        """冗余列 JOIN 与 ST_Contains 连接结果必须逐行一致。

        语义对齐：两者都统计「给定网格集合内的全部该类目 POI」——
        Skill 侧是 grid_id IN (...) JOIN（无额外空间限制），
        手工侧因此也不能加 lon/lat BETWEEN（否则统计口径不同，实测踩坑）。
        """
        out = skill_ctx.tools["spatial_join_agg"].run(SpatialJoinAggInput(
            center=(104.0817, 30.6570), radius_m=1500, time_bucket="workday_evening",
            category_id=1, limit=200), skill_ctx)
        assert out.aggs
        gids = sorted({a.grid_id for a in out.aggs})
        in_list = ",".join(str(g) for g in gids)
        rows = services.ob.execute(
            "SELECT g.grid_id, COUNT(p.poi_id) AS cnt "
            "FROM fact_traffic_grid g JOIN dim_poi p "
            "ON ST_Contains(g.grid_poly, p.location) "
            f"WHERE g.time_bucket='workday_evening' AND p.category_id=1 "
            f"AND g.grid_id IN ({in_list}) "
            "GROUP BY g.grid_id ORDER BY g.grid_id")
        manual = {int(r["grid_id"]): int(r["cnt"]) for r in rows}
        got = {a.grid_id: a.poi_cnt for a in out.aggs}
        assert got.keys() == manual.keys(), "网格集合应一致"
        for gid, cnt in got.items():
            assert cnt == manual[gid], f"grid {gid}: {cnt} != {manual[gid]}"


class TestSiteSelectionScene:
    def test_build_plan_dag(self, skill_ctx):
        plan = skill_ctx.tools["site_selection"].build_plan(skill_ctx)
        ids = [n["id"] for n in plan]
        assert ids == ["s1_candidates", "s2_spatial_join", "s3_score", "s4_reachability"]
        tools = set(skill_ctx.tools)
        for n in plan:
            if n["id"] != "s3_score":
                assert n["skill"] in tools
        # §5.1：可达性排在网格聚合之后（等时圈是第 4 步空间计算的产物，不参与召回排序）
        by_id = {n["id"]: n for n in plan}
        assert by_id["s4_reachability"]["skill"] == "reachability"
        assert by_id["s4_reachability"]["depends_on"] == ["s2_spatial_join"]

    def test_render_spec_valid(self, skill_ctx):
        """render_spec 必须产出架构 §9.3 完整形态（经 contracts 模型校验）。"""
        spec = skill_ctx.tools["site_selection"].render_spec(
            skill_ctx, "pytest-session",
            {"spec": {"center": [104.0817, 30.657], "radius_m": 1500}})
        assert spec["version"] == "1.0"
        kinds = {layer["kind"] for layer in spec["layers"]}
        assert {"poi", "heatmap"} <= kinds
        # 架构 §9.3：source.href 指向后端 GeoJSON 端点；map_view 与 panels 齐备
        for layer in spec["layers"]:
            assert layer["source"]["href"].startswith("/api/sessions/")
        assert spec["map_view"]["center"] is not None
        assert {p["kind"] for p in spec["panels"]} == {"ranking", "metrics"}

    def test_run_end_to_end(self, skill_ctx, db_ok):
        out = skill_ctx.tools["site_selection"].run(SiteSelectionInput(
            center=(104.0817, 30.6570), radius_m=1500, category_id=1,
            keywords=["咖啡"], query_text="安静可自习的咖啡馆"), skill_ctx)
        assert out.candidates
        assert out.summary
        assert {layer["kind"] for layer in out.layers} >= {"poi", "heatmap"}
        assert out.evidence


class TestKnowledgeSearch:
    def test_fulltext_plus_vector(self, skill_ctx, db_ok):
        out = skill_ctx.tools["knowledge_search"].run(KnowledgeSearchInput(
            keywords=["咖啡", "选址"], query_text="咖啡馆选址要点", limit=5), skill_ctx)
        assert out.hits
        assert all("咖啡馆选址" in (h.title or "") for h in out.hits)


class TestIsoLayerAndCamera:
    """场景侧：等时圈图层声明只转发不重造；镜头必须真的装得下那个圆。"""

    def _iso_state(self, skill_ctx):
        from backend.services.spatial_router import PlaceRef
        from backend.skills.atomic.reachability import ReachabilityInput

        iso = skill_ctx.tools["reachability"].run(ReachabilityInput(
            center=PlaceRef(lonlat=(104.0817, 30.657)), minutes=15, mode="driving"), skill_ctx)
        assert iso.ok and iso.degraded                      # conftest 无 key → 正圆降级
        return iso, {"spec": {"center": [104.0817, 30.657], "radius_m": 1500},
                     "isochrone": iso.model_dump(mode="json")}

    def test_scene_forwards_layer_and_widens_camera(self, skill_ctx):
        from backend.api.contracts.map_spec import LayerSpec

        iso, state = self._iso_state(skill_ctx)
        spec = skill_ctx.tools["site_selection"].render_spec(skill_ctx, "s1", state)
        assert {ly["kind"] for ly in spec["layers"]} >= {"poi", "heatmap", "isochrone"}
        # 声明逐字段等于原子 Skill 的产出（场景不重造 href/style）
        got = next(ly for ly in spec["layers"] if ly["kind"] == "isochrone")
        assert got == iso.render_spec
        assert LayerSpec.model_validate(got).source.href.endswith("/layers/l_iso")
        # 正圆半径 5 km（20 km/h × 15 min）：镜头要拉远装圆，但不得低于 z13——
        # 纯「装得下」会到 z10-11，候选簇在市级视野缩成一撮没法判读（实测踩坑）；
        # 真实高德 z13 跨度约 12.8 km，10 km 直径圆整圆入画，候选簇约 90px 可读
        zoom = spec["map_view"]["zoom"]
        assert zoom == 13
        assert zoom < 15                                      # 确实比候选档位拉远了

    def test_without_isochrone_camera_unchanged(self, skill_ctx):
        """没有等时圈时，POI 镜头的经验档位行为一个字节都不变（防回归）。"""
        spec = skill_ctx.tools["site_selection"].render_spec(
            skill_ctx, "s1", {"spec": {"center": [104.0817, 30.657], "radius_m": 1500}})
        assert spec["map_view"]["zoom"] == 15
        assert {ly["kind"] for ly in spec["layers"]} == {"poi", "heatmap"}
