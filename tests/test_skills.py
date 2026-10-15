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
        assert ids == ["s1_candidates", "s2_spatial_join", "s3_score"]
        tools = set(skill_ctx.tools)
        for n in plan:
            if n["id"] != "s3_score":
                assert n["skill"] in tools

    def test_render_spec_valid(self, skill_ctx):
        spec = skill_ctx.tools["site_selection"].render_spec(skill_ctx)
        assert spec["version"] == "1.0"
        kinds = {layer["kind"] for layer in spec["layers"]}
        assert {"points", "heatmap"} <= kinds

    def test_run_end_to_end(self, skill_ctx, db_ok):
        out = skill_ctx.tools["site_selection"].run(SiteSelectionInput(
            center=(104.0817, 30.6570), radius_m=1500, category_id=1,
            keywords=["咖啡"], query_text="安静可自习的咖啡馆"), skill_ctx)
        assert out.candidates
        assert out.summary
        assert {layer["kind"] for layer in out.layers} >= {"points", "heatmap"}
        assert out.evidence


class TestKnowledgeSearch:
    def test_fulltext_plus_vector(self, skill_ctx, db_ok):
        out = skill_ctx.tools["knowledge_search"].run(KnowledgeSearchInput(
            keywords=["咖啡", "选址"], query_text="咖啡馆选址要点", limit=5), skill_ctx)
        assert out.hits
        assert all("咖啡馆选址" in (h.title or "") for h in out.hits)
