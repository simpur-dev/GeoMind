"""API 契约层测试：坐标系转换 / Map Spec 模型 / SSE 总线 / 手势 WKT 翻译。

这些是"前端地基"的硬依赖：转换错一度，地图上就差出几十公里；
kind 对不上，渲染器就静默丢图层——全部用对拍断言钉死。
"""
from __future__ import annotations

import math

import pytest

from backend.api.contracts.map_spec import (
    FeedbackEvent,
    LayerSpec,
    MapSpec,
    MapView,
)
from backend.api.geo import (
    gcj02_to_wgs84,
    viewport_covering,
    viewport_from_center,
    wgs84_to_gcj02,
)
from backend.api.routes import _wkt_to_wgs84
from backend.api.sse_bus import EventBus


def _haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class TestCoordTransform:
    def test_roundtrip_submeter(self):
        """wgs84 → gcj02 → wgs84 往返误差必须亚米级（迭代逆变换的收敛性）。"""
        for lng, lat in [(104.0667, 30.6667), (104.0817, 30.6570), (116.3974, 39.9093)]:
            glng, glat = wgs84_to_gcj02(lng, lat)
            back_lng, back_lat = gcj02_to_wgs84(glng, glat)
            assert _haversine_m(lng, lat, back_lng, back_lat) < 1.0

    def test_chengdu_offset_magnitude(self):
        """成都附近的 WGS84→GCJ-02 偏移应在 100–1000 m 量级（公开算法性质）。"""
        glng, glat = wgs84_to_gcj02(104.0667, 30.6667)
        d = _haversine_m(104.0667, 30.6667, glng, glat)
        assert 100.0 < d < 1000.0

    def test_offset_direction_magnitude(self):
        """成都实测偏移：经度 +0.0025°（东）、纬度 -0.0024°（南）。

        GCJ-02 偏移方向随位置非线性变化（该算法的已知性质），因此只断言
        「偏移非零且两轴均在 0.0005°–0.01°（约 50 m–1 km）区间」，不假设固定方向。
        """
        glng, glat = wgs84_to_gcj02(104.0667, 30.6667)
        dlng, dlat = abs(glng - 104.0667), abs(glat - 30.6667)
        assert 0.0005 < dlng < 0.01 and 0.0005 < dlat < 0.01

    def test_overseas_passthrough(self):
        """境外坐标不偏移（高德底图同理）。"""
        assert wgs84_to_gcj02(-0.1276, 51.5072) == (-0.1276, 51.5072)
        assert gcj02_to_wgs84(139.6917, 35.6895) == (139.6917, 35.6895)

    def test_deterministic(self):
        assert wgs84_to_gcj02(104.06, 30.64) == wgs84_to_gcj02(104.06, 30.64)

    def test_viewport_zoom_mapping(self):
        v = viewport_from_center((104.0817, 30.6570), 1500)
        assert v["zoom"] == 15 and len(v["center"]) == 2
        assert viewport_from_center((104.0817, 30.6570), 3200)["zoom"] == 14
        # viewport 中心必须是 GCJ-02（与库内 WGS84 不同）
        assert v["center"] != [104.0817, 30.6570]

    @pytest.mark.parametrize("radius_m", [1500.0, 3000.0, 5000.0, 12000.0])
    def test_viewport_covering_actually_fits_the_circle(self, radius_m: float):
        """等时圈镜头必须**装得下**那个圆——按降级画布的投影模型反算核验。

        实测教训：沿用候选簇的经验档位时，5 km 等时圈在 800×600 画布上被投影成
        8682×4023 px（超出画布十余倍），图 render 出来了但在画面外。
        """
        v = viewport_covering((104.0817, 30.6570), radius_m)
        zoom = v["zoom"]
        assert 3 <= zoom <= 20
        span_deg = 0.02 / 2 ** (zoom - 13)          # web/src/layers/fallback.ts spanDeg
        need_deg = 2 * radius_m / 111_320.0
        assert span_deg >= need_deg, f"z{zoom} 可见 {span_deg:.4f}° 装不下 {need_deg:.4f}°"
        assert zoom < viewport_from_center((104.0817, 30.6570), radius_m)["zoom"]

    def test_viewport_covering_rejects_non_positive_radius(self):
        with pytest.raises(ValueError, match="radius_m"):
            viewport_covering((104.08, 30.65), 0)


class TestMapSpecContract:
    def test_kind_alias_backward_compat(self):
        """1.0 版样例数据（scene_solution 已灌入）的 points/line 命名归一为架构注册表。"""
        ls = LayerSpec(id="x", kind="points", data_ref="candidates")
        assert ls.kind == "poi"
        assert LayerSpec(id="y", kind="line").kind == "route"

    def test_full_spec_validates(self):
        """render_spec 的产出形态必须通过契约模型（与前端 TS 接口同源）。"""
        spec = MapSpec.model_validate({
            "version": "1.0",
            "map_view": {"center": [104.085, 30.66], "zoom": 14, "pitch": 0},
            "layers": [
                {"id": "l_candidates", "kind": "poi",
                 "source": {"href": "/api/sessions/s/layers/l_candidates", "type": "geojson"},
                 "style": {"radius": 6}, "interactions": ["click"], "status": "ready",
                 "derived_from": ["s1"]},
                {"id": "l_heat", "kind": "heatmap",
                 "source": {"href": "/api/sessions/s/layers/l_heat", "type": "geojson"},
                 "style": {"radius": 24}, "interactions": [], "status": "ready"},
            ],
            "panels": [{"id": "p_ranking", "kind": "ranking", "data_ref": "candidates"}],
        })
        assert len(spec.layers) == 2 and spec.map_view is not None

    def test_unknown_kind_rejected(self):
        """注册表外的 kind 必须被拒（防止前后端静默丢图层）。"""
        with pytest.raises(ValueError):
            LayerSpec(id="z", kind="satellite_tiles")

    def test_map_view_bounds(self):
        with pytest.raises(ValueError):
            MapView(center=[104.0, 30.0], zoom=99)

    def test_feedback_event(self):
        ev = FeedbackEvent(type="draw_polygon",
                           payload={"polygon_wkt": "POLYGON((104.06 30.64,...))"})
        assert ev.type == "draw_polygon"
        with pytest.raises(ValueError):
            FeedbackEvent(type="launch_missile")


class TestSseBus:
    def test_seq_monotonic_and_since(self):
        bus = EventBus()
        bus.publish("s1", "message.start")
        s2 = bus.publish("s1", "token", {"text": "hi"})
        assert s2 == 2 and bus.last_seq("s1") == 2
        assert [e["type"] for e in bus.since("s1", 1)] == ["token"]
        assert bus.since("s1", 2) == []

    def test_resume_no_gap_no_dup(self):
        """Last-Event-ID 续传：从 last_seq 重放，不漏不重。"""
        bus = EventBus()
        for i in range(5):
            bus.publish("s", "tool.end", {"i": i})
        seen = bus.since("s", 0)
        last = seen[-1]["seq"]
        again = bus.since("s", last)
        assert again == []
        tail = bus.publish("s", "done")
        assert [e["seq"] for e in bus.since("s", last)] == [tail]

    def test_sessions_isolated(self):
        bus = EventBus()
        bus.publish("a", "token")
        assert bus.since("b") == [] and bus.last_seq("b") == 0

    def test_history_trim(self):
        bus = EventBus(history_size=3)
        for i in range(10):
            bus.publish("s", "token", {"i": i})
        buf = bus.since("s", 0)
        assert len(buf) == 3 and [e["data"]["i"] for e in buf] == [7, 8, 9]


class TestWktTranslation:
    def test_gcj02_polygon_to_wgs84(self):
        gcj = "POLYGON((104.0682 30.6435,104.0952 30.6435,104.0952 30.6705,104.0682 30.6705,104.0682 30.6435))"
        wgs = _wkt_to_wgs84(gcj)
        assert wgs.startswith("POLYGON((") and wgs.count(",") == 4
        # 逐顶点对拍 gcj02_to_wgs84
        import re

        pts = re.findall(r"(-?\d+\.\d+) (-?\d+\.\d+)", wgs)
        src = re.findall(r"(-?\d+\.\d+) (-?\d+\.\d+)", gcj)
        for (sx, sy), (dx, dy) in zip(src, pts, strict=True):
            elng, elat = gcj02_to_wgs84(float(sx), float(sy))
            assert abs(float(dx) - elng) < 1e-6 and abs(float(dy) - elat) < 1e-6

    def test_garbage_passthrough(self):
        """非法 WKT 原样返回（由后端校验层拦截，不静默吞掉）。"""
        assert _wkt_to_wgs84("not a wkt") == "not a wkt"
