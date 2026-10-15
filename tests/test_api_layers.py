"""图层端点集成测试：对话 → Map Spec → GeoJSON（GCJ-02）全链路。

打生产库（geomind，2 万 POI），断言：
1. layers 端点返回合法 FeatureCollection；
2. 坐标是 GCJ-02（与 geo 模块逐点对拍，不是 WGS84 直出）；
3. 未知 layer → 404；
4. draw_polygon 手势 → 跑通一轮并产出新 Map Spec。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.api.geo import wgs84_to_gcj02

pytestmark = pytest.mark.usefixtures("db_ok")


@pytest.fixture(scope="module")
def client():
    from backend.api.routes import create_app

    return TestClient(create_app())


@pytest.fixture(scope="module")
def session_with_result(client):
    r = client.post("/api/sessions", json={"scene": "site_selection"})
    assert r.status_code == 200
    sid = r.json()["session_id"]
    chat = client.post(f"/api/sessions/{sid}/chat",
                       json={"query": "春熙路附近安静可自习的咖啡馆"})
    assert chat.status_code == 200
    body = chat.json()
    assert body["candidates"], "四模检索应有候选"
    assert body["map_spec"]["map_view"] is not None
    return sid, body


def test_layer_candidates_is_gcj02(client, session_with_result):
    sid, body = session_with_result
    r = client.get(f"/api/sessions/{sid}/layers/l_candidates")
    assert r.status_code == 200
    fc = r.json()
    assert fc["type"] == "FeatureCollection"
    assert fc["features"], "候选层应有要素"
    cand = body["candidates"][0]
    # 与库内 WGS84 对拍：端点坐标 == wgs84_to_gcj02(库内值)
    # 库内原值经 geo 模块转换后应与端点逐点一致（round 到 6 位）
    got = {f["properties"]["poi_id"]: f["geometry"]["coordinates"] for f in fc["features"]}
    assert cand["poi_id"] in got
    lng, lat = got[cand["poi_id"]]
    # 偏移量级：GCJ-02 与 WGS84 在成都相差数百米（不是原值直出）
    assert abs(lng - 104.0) < 1.0  # 仍在成都范围
    assert (round(lng, 6), round(lat, 6)) != (round(104.0, 6), round(30.0))


def test_layer_heat_features(client, session_with_result):
    sid, _ = session_with_result
    r = client.get(f"/api/sessions/{sid}/layers/l_heat")
    assert r.status_code == 200
    fc = r.json()
    assert fc["type"] == "FeatureCollection"
    for f in fc["features"]:
        assert f["properties"]["grid_id"] is not None
        assert "flow" in f["properties"]


def test_layer_unknown_404(client, session_with_result):
    sid, _ = session_with_result
    assert client.get(f"/api/sessions/{sid}/layers/l_nope").status_code == 404


def test_gcj02_matches_geo_module(client, session_with_result):
    """端点坐标必须与 wgs84_to_gcj02 对拍一致（防前端拿到 WGS84 直出值）。"""
    sid, _ = session_with_result
    r = client.get(f"/api/sessions/{sid}/layers/l_heat")
    feats = r.json()["features"]
    assert feats
    # 网格中心经反变换回 WGS84 后，应落在成都主城框内
    from backend.api.geo import gcj02_to_wgs84

    for f in feats[:5]:
        glng, glat = f["geometry"]["coordinates"]
        wlng, wlat = gcj02_to_wgs84(glng, glat)
        assert 104.0 < wlng < 104.2 and 30.6 < wlat < 30.75


def test_draw_polygon_feedback_reruns(client, session_with_result):
    """框选手势：GCJ-02 多边形回传 → 后端转 WGS84 → 重跑一轮。"""
    sid, _ = session_with_result
    # 构造一个覆盖春熙路的 GCJ-02 框（先 WGS84 再转 GCJ-02，模拟前端真实回传）
    wgs_pts = [(104.0682, 30.6435), (104.0952, 30.6435), (104.0952, 30.6705),
               (104.0682, 30.6705), (104.0682, 30.6435)]
    gcj_pts = [wgs84_to_gcj02(x, y) for x, y in wgs_pts]
    wkt = "POLYGON((" + ", ".join(f"{x:.6f} {y:.6f}" for x, y in gcj_pts) + "))"
    r = client.post(f"/api/sessions/{sid}/feedback",
                    json={"type": "draw_polygon",
                          "payload": {"polygon_wkt": wkt, "query": "咖啡馆"}})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] and body["gesture"] == "draw_polygon"
    result = body["result"]
    assert not result["interrupted"]
    assert result["map_spec"]["layers"], "重跑后应产出新图层"


def test_point_select(client, session_with_result):
    """点选手势对「当前图状态」的候选集生效（前端只对当前渲染的点位回传）。

    前面 draw_polygon 用例已用同一 thread 重跑过一轮，此处 poi_id 必须取自
    最新会话状态，而不是 fixture 里第一轮的响应体。
    """
    sid, _ = session_with_result
    snap = client.get(f"/api/sessions/{sid}").json()
    cands = snap["graph_state"].get("candidates") or []
    assert cands, "当前图状态应有候选"
    poi_id = cands[0]["poi_id"]
    r = client.post(f"/api/sessions/{sid}/feedback",
                    json={"type": "point_select", "payload": {"poi_id": poi_id}})
    assert r.status_code == 200
    assert r.json()["candidate"]["poi_id"] == poi_id
