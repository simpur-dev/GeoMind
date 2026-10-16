"""等时圈反哺候选检索的闭环测试（任务卡 5，架构 §7 + §3.1 + §5.1）。

分两层：
  · 单元层用 stub 工具跑真图，零 DB 零网络——为的是能拿计数与入参做硬断言
    （「反哺用了多边形」「一轮只调一次高德」这类事不能靠返回值猜）；
  · 集成层走 TestClient（真 SeekDB，`conftest` 已强制无高德 key → 正圆降级），
    用 SQL 侧 `ST_Contains` 证明**返回的候选真的在那圈里**。
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from backend.brain.graph import _iso_matches, _iso_request, build_graph
from backend.skills.atomic.geocode import PlaceOut
from backend.skills.atomic.multi_modal_search import MultiModalSearchOutput, PoiCandidate
from backend.skills.atomic.reachability import ReachabilityInput, ReachabilityOutput
from backend.skills.registry import SkillRegistry

CENTER = (104.0817, 30.6570)
POLY = "POLYGON((104.07 30.65, 104.09 30.65, 104.09 30.67, 104.07 30.67, 104.07 30.65))"


class StubReachability:
    """假可达性工具：记调用次数与入参，产出可选「有没有多边形」。"""

    def __init__(self, polygon_wkt: str = POLY, ok: bool = True) -> None:
        self.calls: list[ReachabilityInput] = []
        self._wkt = polygon_wkt
        self._ok = ok

    def run(self, inputs: ReachabilityInput, ctx: Any) -> ReachabilityOutput:
        self.calls.append(inputs)
        lon, lat = inputs.center.lonlat or CENTER          # type: ignore[union-attr]
        return ReachabilityOutput(
            ok=self._ok, mode=inputs.mode, minutes=inputs.minutes,
            center=PlaceOut(name="输入点", lon=lon, lat=lat, source="input"),
            polygon_wkt=self._wkt if self._ok else "",
            degraded=True, geojson={}, render_spec={}, elapsed_ms=1.5,
        )


class StubSearch:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def run(self, inputs: Any, ctx: Any) -> MultiModalSearchOutput:
        self.calls.append(inputs)
        return MultiModalSearchOutput(
            candidates=[PoiCandidate(poi_id=1, name="咖啡A")], plan="TABLE RANGE SCAN",
            elapsed_ms=2.0)


class StubOb:
    def __init__(self) -> None:
        self.writes: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.writes.append((sql, params))
        return []

    def execute_timed(self, sql: str, params: dict[str, Any] | None = None
                      ) -> tuple[list[dict[str, Any]], float]:
        return [], 1.0


class StubEmbed:
    def embed_query(self, text: str) -> str:
        return "[0.1,0.2]"

    def embed_doc(self, text: str) -> str:
        return "[0.1,0.2]"


@pytest.fixture()
def stub_ctx() -> Any:
    """真图 + 假工具：feedback 节点的 INSERT 落到 StubOb，不碰库也不碰网络。"""
    from backend.services import ServiceBundle
    from backend.skills.base import SkillContext

    reg = SkillRegistry.instance()
    reg.discover()
    reach = StubReachability()
    search = StubSearch()
    agg_calls: list[Any] = []

    class StubAgg:
        def run(self, inputs: Any, ctx: Any) -> Any:
            agg_calls.append(inputs)
            return SimpleNamespace(aggs=[], elapsed_ms=1.0)

    svc = ServiceBundle(ob=StubOb(), query_builder=object(), embedding=StubEmbed(),
                        amap=None, router=None, state_repo=None, events=None)
    ctx = SkillContext(session_id="t-loop", state={}, services=svc,
                       tools={"multi_modal_search": search, "spatial_join_agg": StubAgg(),
                              "reachability": reach, "site_selection": reg.get("site_selection")})
    return ctx


def run_graph(ctx: Any, query: str, thread: str,
              gesture: dict[str, Any] | None = None) -> dict[str, Any]:
    app = build_graph(ctx)
    out = app.invoke({"session_id": thread, "query": query, "gesture": gesture},
                     {"configurable": {"thread_id": thread}})
    # 措辞不够明确时图会停在 HITL 追问，candidates 根本没跑——必须显式失败，
    # 否则下面的 calls[0] 只会抛一个看不出原因的 IndexError。
    assert "__interrupt__" not in out, f"用例不应停在 HITL：{query}"
    return {k: v for k, v in out.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# 助手判据（纯函数）
# ---------------------------------------------------------------------------
def test_iso_request_uses_skill_schema_defaults_not_a_second_copy():
    from backend.skills.atomic.reachability import ReachabilityInput

    f = ReachabilityInput.model_fields
    req = _iso_request({"center": CENTER})
    assert req == {"center": CENTER, "minutes": int(f["minutes"].default),
                   "mode": str(f["mode"].default), "explicit": False}
    explicit = _iso_request({"center": CENTER, "iso_minutes": 10, "travel_mode": "walking"})
    assert explicit["explicit"] is True and explicit["minutes"] == 10 and explicit["mode"] == "walking"
    assert _iso_request({"center": None}) is None


def test_iso_matches_only_when_all_three_params_agree():
    stored = {"center": {"lon": CENTER[0], "lat": CENTER[1]}, "mode": "walking", "minutes": 10}
    assert _iso_matches(stored, {"center": CENTER, "mode": "walking", "minutes": 10})
    assert not _iso_matches(stored, {"center": CENTER, "mode": "walking", "minutes": 11})
    assert not _iso_matches(stored, {"center": CENTER, "mode": "driving", "minutes": 10})
    assert not _iso_matches(stored, {"center": (105.0, 30.657), "mode": "walking", "minutes": 10})
    assert not _iso_matches({}, {"center": CENTER, "mode": "walking", "minutes": 10})
    assert not _iso_matches(None, {"center": CENTER, "mode": "walking", "minutes": 10})


# ---------------------------------------------------------------------------
# 图内反哺与去重（①②③④⑤）
# ---------------------------------------------------------------------------
def test_explicit_minutes_feed_polygon_into_search(stub_ctx: Any):
    """①显式时间预算 → candidates 先算圈，再把 WGS84 多边形喂进检索；圆约束让位。"""
    state = run_graph(stub_ctx, "春熙路步行10分钟内的咖啡馆", "t-loop-feed")
    called = stub_ctx.tools["multi_modal_search"].calls[0]          # type: ignore[attr-defined]
    assert called.polygon_wkt == POLY
    assert called.center is None and called.radius_m is None       # 二选一契约，不双谓词
    assert state["isochrone"]["mode"] == "walking" and state["isochrone"]["minutes"] == 10
    assert "iso_filter_skip" not in state


def test_without_explicit_minutes_behaviour_is_byte_identical(stub_ctx: Any):
    """②没给时间预算 → 检索仍用 center+radius，等时圈只在后面做展示。"""
    state = run_graph(stub_ctx, "帮我在春熙路附近选个咖啡馆位置", "t-loop-plain")
    called = stub_ctx.tools["multi_modal_search"].calls[0]          # type: ignore[attr-defined]
    assert called.polygon_wkt is None
    assert tuple(called.center) == pytest.approx(CENTER)
    assert called.radius_m == 1500
    assert state["isochrone"]["mode"] == "driving" and state["isochrone"]["minutes"] == 15
    assert "iso_filter_skip" not in state


def test_reachability_called_exactly_once_per_round(stub_ctx: Any):
    """③去重：candidates 产出后 spatial_join 复用，一轮只发一次高德请求。"""
    state = run_graph(stub_ctx, "春熙路开车20分钟的咖啡馆", "t-loop-dedupe")
    reach: StubReachability = stub_ctx.tools["reachability"]        # type: ignore[attr-defined]
    assert len(reach.calls) == 1
    assert state["isochrone"]["minutes"] == 20 and state["isochrone"]["mode"] == "driving"


def test_empty_polygon_falls_back_to_circle_and_marks_skip(stub_ctx: Any):
    """④没多边形 → 回落 center+radius，且把原因写进 state（检索宁可宽不可空）。"""
    stub_ctx.tools["reachability"] = StubReachability(polygon_wkt="")  # type: ignore[index]
    state = run_graph(stub_ctx, "春熙路步行10分钟内的咖啡馆", "t-loop-skip")
    called = stub_ctx.tools["multi_modal_search"].calls[0]         # type: ignore[attr-defined]
    assert called.polygon_wkt is None and called.radius_m == 1500   # 回落成功
    assert state["iso_filter_skip"] == "no_polygon"


def test_gesture_polygon_wins_over_isochrone(stub_ctx: Any):
    """⑤同一轮里用户画的框优先，等时圈只展示不参与筛选（D3）。"""
    stub_ctx.tools["reachability"] = StubReachability()             # type: ignore[index]
    gesture = {"polygon_wkt": "POLYGON((104.0 30.6, 104.1 30.6, 104.1 30.7,"
                              " 104.0 30.7, 104.0 30.6))"}
    out = run_graph(stub_ctx, "帮我在春熙路附近选个咖啡馆位置，步行10分钟",
                    "t-loop-gesture", gesture=gesture)
    called = stub_ctx.tools["multi_modal_search"].calls[-1]         # type: ignore[attr-defined]
    assert called.polygon_wkt == gesture["polygon_wkt"]             # 手势框原样，未被等时圈顶掉
    assert out["iso_filter_skip"] == "gesture_polygon"
    assert out["isochrone"]["mode"] == "walking"                   # 圈仍算出来供展示


# ---------------------------------------------------------------------------
# 场景直调路径（§5.1 双入口一致）
# ---------------------------------------------------------------------------
def test_scene_run_path_produces_same_isochrone_layer(skill_ctx: Any, db_ok: Any):
    from backend.skills.scenes.site_selection import SiteSelectionInput

    scene = skill_ctx.tools["site_selection"]
    out = scene.run(SiteSelectionInput(center=CENTER, radius_m=1500, category_id=1,
                                       keywords=["咖啡"], iso_minutes=10,
                                       travel_mode="walking"), skill_ctx)
    kinds = {ly["kind"] for ly in out.layers}
    assert "isochrone" in kinds
    assert any(ev["ref"] == "distance_matrix" and "10 min walking" in ev["note"]
               for ev in out.evidence), out.evidence
    # 不给时间预算 → 不产等时圈图层（与 graph 的「默认只展示」区分开：直调是显式选择）
    plain = scene.run(SiteSelectionInput(center=CENTER, radius_m=1500, category_id=1,
                                         keywords=["咖啡"]), skill_ctx)
    assert "isochrone" not in {ly["kind"] for ly in plain.layers}


# ---------------------------------------------------------------------------
# 集成（真库 + 免 key 正圆降级）：候选必须真落在等时圈里
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from backend.api.routes import create_app
    return TestClient(create_app())


def test_integrated_walk_ten_minutes_candidates_are_inside_polygon(client, services, db_ok):
    sid = client.post("/api/sessions", json={"scene": "site_selection"}).json()["session_id"]
    body = client.post(f"/api/sessions/{sid}/chat",
                       json={"query": "春熙路步行10分钟内的咖啡馆"}).json()
    assert not body["interrupted"] and body["candidates"], "闭环查询应产出候选"

    st = client.get(f"/api/sessions/{sid}").json()["graph_state"]
    assert st["spec"]["iso_minutes"] == 10 and st["spec"]["travel_mode"] == "walking"
    assert st["isochrone"]["polygon_wkt"].startswith("POLYGON((")
    assert any(ly["kind"] == "isochrone" for ly in st["map_spec"]["layers"])

    # 硬证：库里逐条判定「候选不在圈内」的数量必须是 0（polygon 与坐标同为 WGS84）
    ids = [c["poi_id"] for c in body["candidates"]]
    ph = ", ".join(f"%(i{i})s" for i in range(len(ids)))
    params: dict[str, Any] = {f"i{i}": v for i, v in enumerate(ids)}
    params["wkt"] = st["isochrone"]["polygon_wkt"]
    outside = services.ob.execute(
        "SELECT COUNT(*) AS n FROM dim_poi "
        f"WHERE poi_id IN ({ph}) "
        "AND NOT ST_Contains(ST_GeomFromText(%(wkt)s), location)", params)[0]["n"]
    assert int(outside) == 0, f"{outside} 个候选落在等时圈外 → 反哺没生效"


def test_integrated_plain_query_does_not_filter_by_isochrone(client, db_ok):
    sid = client.post("/api/sessions", json={"scene": "site_selection"}).json()["session_id"]
    body = client.post(f"/api/sessions/{sid}/chat",
                       json={"query": "帮我在春熙路附近选个咖啡馆位置"}).json()
    assert not body["interrupted"], "低置信度会走 HITL，用例要的是完整跑完的一轮"
    st = client.get(f"/api/sessions/{sid}").json()["graph_state"]
    assert st["spec"]["iso_minutes"] is None
    assert "iso_filter_skip" not in st
    assert st["isochrone"]["minutes"] == 15 and st["isochrone"]["mode"] == "driving"
    assert any(ly["kind"] == "isochrone" for ly in st["map_spec"]["layers"]), "默认圈仍要展示"
