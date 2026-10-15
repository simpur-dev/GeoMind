"""LangGraph 7 步测试（设计文档 §8 第 4 行：正常路径 + 中断恢复）。"""
from __future__ import annotations

from backend.brain.graph import GRAPH_NODES, resume_query, run_query


class TestGraph:
    def test_full_path(self, skill_ctx, db_ok):
        r = run_query("春熙路附近安静可自习的咖啡馆", skill_ctx, thread_id="pytest-full")
        assert not r["interrupted"]
        st = r["state"]
        assert st["intent"]["scene"] in ("hybrid_search", "site_selection")
        assert tuple(st["spec"]["center"]) == (104.0817, 30.657)
        assert st["candidates"], "候选不应为空"
        assert all(c["dist_m"] <= 1500 for c in st["candidates"])
        assert st["aggs"]
        assert st["decision"]["summary"]
        assert {layer["kind"] for layer in st["map_spec"]["layers"]} >= {"points", "heatmap"}
        assert st["feedback"]["written"]

    def test_interrupt_then_resume(self, skill_ctx, db_ok):
        tid = "pytest-ask"
        r1 = run_query("找个安静能自习的咖啡馆", skill_ctx, thread_id=tid)
        assert r1["interrupted"]
        assert r1["question"]["question"]

        r2 = resume_query("春熙路", skill_ctx, thread_id=tid)
        assert not r2["interrupted"]
        assert r2["state"]["intent"]["slots"]["center"] == [104.0817, 30.657]
        assert r2["state"]["candidates"]

    def test_graph_has_seven_nodes(self, skill_ctx):
        from backend.brain.graph import build_graph

        app = build_graph(skill_ctx)
        assert app.get_graph().nodes.keys() >= set(GRAPH_NODES)

    def test_scene_routing_to_site_selection(self, skill_ctx, db_ok):
        r = run_query("帮我在建设路附近选个咖啡馆位置", skill_ctx, thread_id="pytest-scene")
        assert not r["interrupted"]
        assert r["state"]["intent"]["scene"] == "site_selection"
