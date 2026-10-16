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
        assert {layer["kind"] for layer in st["map_spec"]["layers"]} >= {"poi", "heatmap"}
        assert st["map_spec"]["map_view"]["center"] is not None
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
        nodes = set(app.get_graph().nodes) - {"__start__", "__end__"}
        assert nodes >= set(GRAPH_NODES)
        # 骨架冻结核对（§5.6）：接了 reachability 也不许多出第 8 个业务节点
        assert nodes == set(GRAPH_NODES)

    def test_spatial_join_lands_degraded_isochrone_in_state(self, skill_ctx, db_ok):
        """任务卡 2：等时圈产出进图状态 + 场景声明 l_iso 图层（conftest 无 key → 正圆降级）。"""
        r = run_query("帮我在春熙路附近选个咖啡馆位置", skill_ctx, thread_id="pytest-iso")
        st = r["state"]
        iso = st.get("isochrone") or {}
        assert iso and iso["ok"] and iso["degraded"] is True
        assert iso["sampled_points"] == 0 and len(iso["vertices"]) == 12
        ring = iso["geojson"]["features"][0]["geometry"]["coordinates"][0]
        assert len(ring) == 13 and ring[0] == ring[-1]      # GCJ-02 闭合环

        kinds = {layer["kind"] for layer in st["map_spec"]["layers"]}
        assert "isochrone" in kinds
        layer = next(ly for ly in st["map_spec"]["layers"] if ly["kind"] == "isochrone")
        assert layer["id"] == "l_iso" and layer["source"]["href"].endswith("/layers/l_iso")
        assert layer["source"]["type"] == "geojson" and layer["status"] == "ready"
        # 场景不得重造声明：与原子 Skill 的产出逐字段一致
        assert layer == iso["render_spec"]

    def test_scene_routing_to_site_selection(self, skill_ctx, db_ok):
        r = run_query("帮我在建设路附近选个咖啡馆位置", skill_ctx, thread_id="pytest-scene")
        assert not r["interrupted"]
        assert r["state"]["intent"]["scene"] == "site_selection"

    def test_polygon_gesture_translates_center(self):
        """框选手势 → L1 翻译出质心+外接半径（网格聚合层的圆路径依赖它）。"""
        from backend.brain.intent import RuleIntentParser

        wkt = "POLYGON((104.06 30.64, 104.10 30.64, 104.10 30.67, 104.06 30.67, 104.06 30.64))"
        r = RuleIntentParser().parse("咖啡馆", gesture={"polygon_wkt": wkt})
        slots = r.slots
        assert slots["polygon_wkt"] == wkt
        assert slots["center"] == [104.08, 30.655]          # 顶点均值质心
        assert 2400 < slots["radius_m"] < 2700              # ≈ 质心到框角的外接半径
        assert not r.question                                # 手势给了空间约束，不追问
        # 非法 WKT：不派生 center（回落到文本/追问路径），不抛异常
        r2 = RuleIntentParser().parse("咖啡馆", gesture={"polygon_wkt": "POLYGON((bad))"})
        assert "center" not in r2.slots or r2.slots.get("center_name") != "框选区域"
