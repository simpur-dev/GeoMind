"""QueryBuilder 卡点测试（对应设计文档 §8 测试矩阵第 1 行）。"""
from __future__ import annotations

import pytest

from backend.query_builder import (
    CompileError,
    GridAggSpec,
    HybridQuerySpec,
    PlanAssertionError,
    QueryBuilder,
    ScalarConstraint,
    SemanticConstraint,
    SpatialConstraint,
    SQLValidationError,
)

QVEC = "[" + ",".join(["0.5"] * 1024) + "]"


def _spec(**kw) -> HybridQuerySpec:
    base = dict(
        spatial=SpatialConstraint(center=(104.0817, 30.6570), radius_m=1500),
        scalar=ScalarConstraint(category_id=1, min_rating=4.0, status=1),
        semantic=SemanticConstraint(keywords=["咖啡"], query_text="安静可自习的咖啡馆", limit=10),
    )
    base.update(kw)
    return HybridQuerySpec(**base)


class TestCompile:
    def test_two_stage_spatial(self, services):
        """空间谓词必须配 MBR（两段式）。"""
        qb = QueryBuilder(services.ob)
        cq = qb.compile_hybrid(_spec(), QVEC)
        assert cq.has_spatial and cq.has_semantic
        assert "lon BETWEEN" in cq.sql and "lat BETWEEN" in cq.sql
        assert "_ST_Dwithin" in cq.sql
        assert "APPROXIMATE" in cq.sql
        assert "MATCH(" in cq.sql

    def test_params_are_placeholders(self, services):
        """SQL 不含字面量用户输入，全部 %(name)s 占位。"""
        qb = QueryBuilder(services.ob)
        cq = qb.compile_hybrid(_spec(), QVEC)
        for banned in ("104.0817", "30.6570", "咖啡", "自习", "1500"):
            assert banned not in cq.sql, f"{banned} 不应出现在 SQL 文本中"
        assert set(cq.params) >= {"center_wkt", "r_deg", "x0", "x1", "y0", "y1",
                                  "match_expr", "qvec", "limit"}

    def test_qvec_dim_must_match(self, services):
        qb = QueryBuilder(services.ob)
        with pytest.raises(CompileError):
            qb.compile_hybrid(_spec(), "[0.1,0.2]")

    def test_query_text_without_qvec_rejected(self, services):
        qb = QueryBuilder(services.ob)
        with pytest.raises(CompileError):
            qb.compile_hybrid(_spec(), None)

    def test_polygon_wkt(self, services):
        qb = QueryBuilder(services.ob)
        wkt = "POLYGON((104.06 30.64,104.10 30.64,104.10 30.68,104.06 30.68,104.06 30.64))"
        # 无语义条件（否则需提供 qvec）
        spec = HybridQuerySpec(
            spatial=SpatialConstraint(polygon_wkt=wkt),
            scalar=ScalarConstraint(category_id=1),
            semantic=SemanticConstraint(keywords=["咖啡"], limit=10),
        )
        cq = qb.compile_hybrid(spec, None)
        assert cq.has_spatial and "ST_Contains" in cq.sql
        assert cq.params["x0"] == 104.06 and cq.params["x1"] == 104.10

    def test_grid_agg_uses_grid_id_join(self, services):
        """网格聚合必须走 grid_id 冗余列 JOIN，不用 ST_Contains。"""
        qb = QueryBuilder(services.ob)
        cq = qb.compile_grid_agg(GridAggSpec(center=(104.0817, 30.6570), radius_m=1500,
                                            time_bucket="workday_evening"))
        assert "p.grid_id = g.grid_id" in cq.sql
        assert "ST_Contains" not in cq.sql
        assert "MERGE JOIN" in cq.expected_operators

    def test_two_stage_fallback_compiles(self, services):
        qb = QueryBuilder(services.ob)
        cq = qb.compile_two_stage(_spec(), QVEC)
        assert cq.sql.upper().startswith("WITH CAND AS")
        assert "ORDER BY cosine_distance" in cq.sql


class TestValidation:
    def test_injection_in_keyword_rejected(self, services):
        """注入关键词必须被字符白名单拒绝。"""
        qb = QueryBuilder(services.ob)
        spec = _spec(semantic=SemanticConstraint(
            keywords=["咖啡'; DROP TABLE dim_poi; --"], query_text="x", limit=5))
        with pytest.raises(CompileError):
            qb.compile_hybrid(spec, QVEC)

    def test_injection_in_attrs_key_rejected(self, services):
        qb = QueryBuilder(services.ob)
        spec = _spec(scalar=ScalarConstraint(category_id=1, attrs_filter={"a'b": 1}))
        with pytest.raises(CompileError):
            qb.compile_hybrid(spec, None)

    def test_multi_statement_rejected(self, services):
        qb = QueryBuilder(services.ob)
        cq = qb.compile_hybrid(_spec(), QVEC)
        cq.sql = cq.sql + "; DROP TABLE dim_poi"
        with pytest.raises(SQLValidationError) as ei:
            qb.validate(cq)
        assert ei.value.code == "MULTI_STATEMENT"

    def test_write_keyword_rejected(self, services):
        qb = QueryBuilder(services.ob)
        cq = qb.compile_hybrid(_spec(), QVEC)
        cq.sql = cq.sql.replace("SELECT", "DELETE", 1)
        with pytest.raises(SQLValidationError) as ei:
            qb.validate(cq)
        assert ei.value.code == "NOT_READONLY"

    def test_select_star_rejected(self, services):
        qb = QueryBuilder(services.ob)
        cq = qb.compile_hybrid(_spec(), QVEC)
        cq.sql = "SELECT * FROM dim_poi LIMIT 1"
        with pytest.raises(SQLValidationError) as ei:
            qb.validate(cq)
        assert ei.value.code == "SELECT_STAR"

    def test_cross_table_column_rejected(self, services):
        """跨表串列必须被列白名单拒绝（JOIN 查询需传表元组）。"""
        qb = QueryBuilder(services.ob)
        cq = qb.compile_hybrid(_spec(), QVEC)
        # 真正引用另一张表的列（只加 FROM 不引用列是抓不到的，实测踩坑）
        cq.sql = cq.sql.replace("SELECT poi_id", "SELECT poi_id, rent", 1)
        cq.sql = cq.sql.replace("FROM dim_poi", "FROM dim_poi, fact_store", 1)
        cq.tables = ("dim_poi",)  # 故意不声明 fact_store
        with pytest.raises(SQLValidationError) as ei:
            qb.validate(cq)
        assert ei.value.code == "UNKNOWN_COLUMN"

    def test_limit_too_large_rejected(self, services):
        qb = QueryBuilder(services.ob)
        cq = qb.compile_hybrid(_spec(), QVEC)
        cq.params["limit"] = 100000
        with pytest.raises(SQLValidationError) as ei:
            qb.validate(cq)
        assert ei.value.code == "LIMIT_TOO_LARGE"


class TestPlanAssertion:
    def test_plan_assertion_raises_with_actual_plan(self, services, db_ok):
        """断言失败必须携带实际计划（供上层降级决策）。"""
        qb = QueryBuilder(services.ob)
        cq = qb.compile_hybrid(_spec(), QVEC)
        cq.expected_operators = ("NO_SUCH_OPERATOR",)
        with pytest.raises(PlanAssertionError) as ei:
            qb.assert_plan(cq)
        assert "Query Plan" in ei.value.actual_plan or "|0 |" in ei.value.actual_plan

    def test_build_and_check_passes_on_production(self, services, db_ok):
        """生产库上完整三层卡点必须通过。"""
        qb = QueryBuilder(services.ob)
        cq, plan = qb.build_and_check(_spec(), QVEC)
        assert any(op in plan for op in cq.expected_operators)
