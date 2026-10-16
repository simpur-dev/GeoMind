"""三个地理原子 Skill 的契约与降级测试（架构 §5.2 / §2.2 L3）。

铁律 6：不打真实外部服务（MockTransport + OB 桩）；真 SeekDB 只在 db_ok 下跑
「dim_poi 名称兜底对拍」与 §7「等时圈多边形 → OB ST_Contains 批量过滤」闭环。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from test_spatial_router import ObStub, _distance_handler, _driving_handler

from backend.api.contracts.map_spec import LayerSpec
from backend.app import build_skill_context
from backend.config import Settings
from backend.query_builder import QueryBuilder
from backend.services import ServiceBundle
from backend.services.amap_gateway import AmapGateway
from backend.services.spatial_router import PlaceRef, SpatialRouter
from backend.skills.atomic.geocode import GeocodeInput
from backend.skills.atomic.path_planning import PathPlanningInput
from backend.skills.atomic.reachability import ReachabilityInput
from backend.skills.registry import SkillRegistry

NEW_SKILLS = {"geocode", "path_planning", "reachability"}
_CHUNXI = {"春熙路": (104.0817, 30.6570), "天府广场": (104.0665, 30.6575)}


def make_ctx(handler: Any = None, *, key: str = "test-key",
             rows: list[dict[str, Any]] | None = None,
             place_dict: dict[str, tuple[float, float]] | None = None,
             session_id: str = "pytest-geo") -> Any:
    """一次性装配 SkillContext：router / gateway / query_builder 全部可注入 fake。"""
    s = Settings(amap_key=key, amap_qps=50)
    ob = ObStub(rows if rows is not None else [])
    amap = AmapGateway(s, transport=httpx.MockTransport(handler) if handler else None)
    qb = QueryBuilder(ob, s)
    svc = ServiceBundle(ob=ob, query_builder=qb, embedding=None, amap=amap,
                        router=SpatialRouter(amap, s, qb, place_dict=place_dict))
    return build_skill_context(session_id, svc)


# ---------------------------------------------------------------------------
# 注册与 MCP 自动暴露（验收项）
# ---------------------------------------------------------------------------
def test_three_new_atomic_skills_registered():
    reg = SkillRegistry.instance()
    reg.discover()
    assert NEW_SKILLS.issubset(set(reg.skills))
    assert NEW_SKILLS.issubset(set(reg.atomic()))
    described = {d["name"]: d for d in reg.describe_all()}
    for name in NEW_SKILLS:
        assert described[name]["inputSchema"]["type"] == "object"
        assert described[name]["inputSchema"]["properties"]
        assert described[name]["description"] and described[name]["scene"] == "atomic"


def test_nested_placeref_schema_is_exported_for_mcp():
    """PlaceRef 作为嵌套模型必须导出 $defs，否则 MCP 侧拿不到可校验的入参形态。"""
    reg = SkillRegistry.instance()
    reg.discover()
    schema = next(d for d in reg.describe_all() if d["name"] == "path_planning")["inputSchema"]
    assert {"mode", "origin", "destination"} <= set(schema["properties"])
    assert "PlaceRef" in str(schema.get("$defs", {}))


# ---------------------------------------------------------------------------
# geocode
# ---------------------------------------------------------------------------
def test_geocode_skill_offline_place_dict_marks_degraded():
    ctx = make_ctx(key="", place_dict=_CHUNXI)
    out = ctx.tools["geocode"].run(GeocodeInput(address="春熙路"), ctx)
    assert out.found and out.source == "place_dict" and out.degraded is True
    assert (out.lon, out.lat) == (104.0817, 30.6570)
    assert out.evidence == [{"type": "data", "ref": "PLACE_COORDS",
                             "note": "意图地名词表兜底（离线 WGS84）"}]


def test_geocode_skill_offline_dim_poi_evidence_is_sql():
    rows = [{"poi_id": 6469, "name": "环球中心24h健身", "address": "成都市武侯区光华大道372号",
             "city": "成都", "lon": 104.345518, "lat": 30.593414}]
    ctx = make_ctx(key="", rows=rows, place_dict={})
    out = ctx.tools["geocode"].run(GeocodeInput(address="环球中心", city="成都"), ctx)
    assert out.found and out.source == "dim_poi" and out.degraded is True
    assert out.poi_id == 6469 and out.gcj02 is not None and out.gcj02[0] != out.lon
    assert out.evidence[0]["type"] == "sql" and out.evidence[0]["ref"] == "compile_poi_by_name"


def test_geocode_skill_all_miss_returns_honest_empty():
    """验收：AmapUnavailable 不外泄到图执行——Skill 返回 found=False + reason，不抛异常。"""
    ctx = make_ctx(key="", place_dict={})
    out = ctx.tools["geocode"].run(GeocodeInput(address="不存在的地方"), ctx)
    assert out.found is False and out.source == "none" and out.degraded is True
    assert out.lon is None and out.reason and out.evidence[0]["type"] == "data"


def test_geocode_skill_online_not_degraded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "1", "geocodes": [{"location": "104.0817,30.6570"}]})

    ctx = make_ctx(handler)
    out = ctx.tools["geocode"].run(GeocodeInput(address="春熙路"), ctx)
    assert out.found and out.source == "amap" and out.degraded is False
    assert out.gcj02 == (104.0817, 30.657) and (out.lon, out.lat) != out.gcj02
    assert out.evidence[0]["type"] == "amap" and out.elapsed_ms > 0


# ---------------------------------------------------------------------------
# path_planning
# ---------------------------------------------------------------------------
def test_path_planning_skill_offline_refuses_straight_line():
    ctx = make_ctx(key="", place_dict=_CHUNXI)
    out = ctx.tools["path_planning"].run(PathPlanningInput(
        mode="driving", origin=PlaceRef(name="春熙路"), destination=PlaceRef(name="天府广场")), ctx)
    assert out.ok is False and out.degraded is True
    assert out.distance_m is None and out.polyline == [] and out.point_count == 0
    assert "空间谎言" in out.reason and out.evidence[0]["type"] == "amap"
    assert out.origin is None and out.elapsed_ms >= 0


def test_path_planning_skill_online_outputs_wgs84_polyline():
    ctx = make_ctx(_driving_handler)
    out = ctx.tools["path_planning"].run(PathPlanningInput(
        mode="driving", origin=PlaceRef(lonlat=(104.08, 30.65)),
        destination=PlaceRef(lonlat=(104.10, 30.67))), ctx)
    assert out.ok and out.degraded is False
    assert out.distance_m == 4200.0 and out.duration_s == 780.0
    assert out.point_count == len(out.polyline) == 4
    assert out.origin is not None and out.origin.source == "input"
    assert out.polyline[0] != (104.08, 30.65)              # 出口 WGS84，不是高德原值
    assert out.evidence[0]["ref"] == "route_plan"


def test_path_planning_mode_is_constrained_at_input_schema():
    """非高德支持的方式在 Schema 层就被拒（不产生外部请求，也不进 Skill.run）。"""
    with pytest.raises(ValidationError, match="driving"):
        PathPlanningInput(mode="transit", origin=PlaceRef(lonlat=(104.08, 30.65)),
                          destination=PlaceRef(lonlat=(104.06, 30.66)))


# ---------------------------------------------------------------------------
# reachability
# ---------------------------------------------------------------------------
def test_reachability_skill_offline_circle_degraded():
    ctx = make_ctx(key="", place_dict=_CHUNXI)
    out = ctx.tools["reachability"].run(ReachabilityInput(
        center=PlaceRef(name="春熙路"), minutes=15, mode="driving"), ctx)
    assert out.ok is True and out.degraded is True and out.sampled_points == 0
    assert len(out.vertices) == 12 and out.under_sampled == 0 and out.saturated is False
    assert all(v.radius_m == pytest.approx(5000.0, abs=1.0) for v in out.vertices)
    assert out.evidence[0]["ref"] == "iso_circle_approx"
    assert out.center is not None and out.center.source == "place_dict"


def test_reachability_bicycling_rejected_at_schema_level():
    """高德距离测量无骑行档：Skill 入参 Schema 就拒，不会静默套一个错误的 type。"""
    with pytest.raises(ValidationError, match="driving|walking"):
        ReachabilityInput(center=PlaceRef(lonlat=(104.0817, 30.657)), mode="bicycling")


def test_reachability_skill_online_sampled_and_saturated():
    ctx = make_ctx(_distance_handler())
    out = ctx.tools["reachability"].run(ReachabilityInput(
        center=PlaceRef(lonlat=(104.0817, 30.6570)), minutes=15), ctx)
    assert out.ok and out.degraded is False and out.sampled_points == 60
    assert out.saturated is True and out.under_sampled == 0
    assert [v.bearing_deg for v in out.vertices] == [float(b) for b in range(0, 360, 30)]
    assert out.evidence[0]["ref"] == "distance_matrix"


def test_reachability_wkt_forms_are_closed_and_differ_by_crs():
    ctx = make_ctx(key="", place_dict=_CHUNXI)
    out = ctx.tools["reachability"].run(ReachabilityInput(
        center=PlaceRef(name="春熙路"), minutes=10), ctx)
    for wkt in (out.polygon_wkt, out.polygon_wkt_gcj02):
        assert wkt.startswith("POLYGON((") and wkt.endswith("))")
        pairs = wkt[len("POLYGON(("):-2].split(", ")
        assert len(pairs) == 13 and pairs[0] == pairs[-1]      # 12 顶点 + 闭合
    assert out.polygon_wkt != out.polygon_wkt_gcj02


def test_reachability_render_spec_is_valid_map_spec_layer():
    """render_spec 必须过 §9.3 前端契约模型（kind=isochrone、source.type=geojson）。"""
    ctx = make_ctx(key="", place_dict=_CHUNXI, session_id="sess-42")
    out = ctx.tools["reachability"].run(ReachabilityInput(
        center=PlaceRef(name="春熙路"), minutes=10), ctx)
    layer = LayerSpec.model_validate(out.render_spec)          # 反向校验：契约模型可解析
    assert layer.kind == "isochrone" and layer.status == "ready"
    assert layer.source is not None and layer.source.type == "geojson"
    assert layer.source.href == "/api/sessions/sess-42/layers/l_iso"
    assert layer.data_ref == "isochrone"
    # 颜色键面向 AMap 拆成 hex + 独立 opacity（rgba() 串实测整个面不可见）
    assert layer.style["fill"].startswith("#") and 0 < layer.style["fillOpacity"] < 1
    assert layer.interactions == [] and layer.derived_from == ["reachability"]
    ring = out.geojson["features"][0]["geometry"]["coordinates"][0]
    assert len(ring) == 13 and out.geojson["features"][0]["properties"]["degraded"] is True


def test_reachability_skill_api_error_is_honest_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "0", "infocode": "10004",
                                         "info": "DAILY_QUERY_OVER_LIMIT"})

    ctx = make_ctx(handler)
    out = ctx.tools["reachability"].run(ReachabilityInput(
        center=PlaceRef(lonlat=(104.0817, 30.6570)), minutes=15), ctx)
    assert out.ok is False and out.degraded is True and out.vertices == []
    assert "等时圈采样失败" in out.reason
    assert out.evidence[0]["note"] == "高德在线采样失败，不产出等时圈"


# ---------------------------------------------------------------------------
# 铁律静态守卫（结构约束，不靠人记）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mod", sorted(NEW_SKILLS))
def test_skills_do_not_own_sql_or_coord_math(mod: str):
    """铁律 1/2：Skill 不拼 SQL、不做坐标转换/几何——这些都归 L4 路由层。"""
    src = Path("backend/skills/atomic") / f"{mod}.py"
    text = src.read_text(encoding="utf-8")
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.Attribute):
            called.add(node.attr)
    forbidden = {"backend.api.geo", "backend.db.connection", "backend.query_builder"}
    assert not (imported & forbidden), f"{mod} 直接依赖 L4 内部件: {imported & forbidden}"
    for banned in ("gcj02_to_wgs84", "wgs84_to_gcj02", "execute", "execute_timed",
                   "compile_poi_by_name", "distance_matrix", "route_plan"):
        assert banned not in called, f"{mod} 调用了 {banned}（属路由层职责）"
    assert "SELECT" not in text


def test_router_does_own_the_conversion_positive_control():
    """上一条的对照：同一禁令在 L4 路由层确实「有转换、有网关调用」，证明守卫不是空断言。"""
    text = Path("backend/services/spatial_router.py").read_text(encoding="utf-8")
    assert "gcj02_to_wgs84(" in text and "wgs84_to_gcj02(" in text
    assert "distance_matrix(" in text and "compile_poi_by_name(" in text


def make_offline_ctx(services: Any, place_dict: dict[str, tuple[float, float]] | None = None,
                     session_id: str = "geo-db") -> Any:
    """真库 + 强制无 key 的 SkillContext（复用 services 的真实 ob/query_builder，只换网关与路由）。

    不能用 session 级 `services` 原样跑 geocode：根 .env 里有真实高德 key，
    那会打真实 API（铁律 6）。这里把 amap_key 置空，路由层 available=False，零外部请求。
    """
    s = Settings(amap_key="", amap_qps=50)
    amap = AmapGateway(s)
    router = SpatialRouter(amap, s, services.query_builder, place_dict=place_dict)
    return build_skill_context(session_id, services.model_copy(update={"amap": amap, "router": router}))


# ---------------------------------------------------------------------------
# 真库闭环（SeekDB 不可达自动 skip）
# ---------------------------------------------------------------------------
def test_dim_poi_fallback_matches_handwritten_sql(services, db_ok):
    """库内兜底与手写 LIKE 对拍同一 poi_id（QueryBuilder 产物不掺水）。"""
    name = "环球中心"
    ctx = make_offline_ctx(services, place_dict={})
    out = ctx.tools["geocode"].run(GeocodeInput(address=name), ctx)
    rows = services.ob.execute(
        "SELECT poi_id, name, lon, lat FROM dim_poi "
        "WHERE status = 1 AND city = '成都' AND name LIKE %(p)s "
        "ORDER BY rating DESC, poi_id ASC LIMIT 5", {"p": f"%{name}%"})
    assert rows, "样例库应有该名称 POI（seed_cd PLACES 含「环球中心」）"
    assert out.found and out.source == "dim_poi", f"兜底未命中但手写 SQL 命中 {len(rows)} 行: {out.reason}"
    assert out.poi_id == int(rows[0]["poi_id"])
    assert (out.lon, out.lat) == pytest.approx((float(rows[0]["lon"]), float(rows[0]["lat"])))


def test_isochrone_polygon_pushes_down_to_st_contains(services, skill_ctx, db_ok):
    """架构 §7「高德出多边形 → OB ST_Contains 批量过滤」闭环（本轮用正圆降级版验证通路）。"""
    from backend.skills.atomic.multi_modal_search import MultiModalSearchInput

    geo_ctx = make_offline_ctx(services, place_dict=_CHUNXI)
    iso = geo_ctx.tools["reachability"].run(ReachabilityInput(
        center=PlaceRef(name="春熙路"), minutes=6, mode="driving"), geo_ctx)   # 半径 3 km
    assert iso.ok and iso.degraded

    out = skill_ctx.tools["multi_modal_search"].run(MultiModalSearchInput(
        polygon_wkt=iso.polygon_wkt, category_id=1, limit=10), skill_ctx)
    assert out.candidates, "等时圈内应有咖啡类 POI"
    assert "ST_Contains" in out.sql and "BETWEEN" in out.sql      # MBR 预筛 + 精确复核未破
    assert "TABLE" in out.plan                                   # 三层卡点里的 EXPLAIN 断言已过
    assert out.degraded is False
