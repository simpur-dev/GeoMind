"""REST / SSE 路由（架构 §9.3 前后端交互契约）。

通道平等观：Web / CLI / HTTP / MCP 挂在同一套内核契约上（架构 §9.1）。
图层数据端点返回 **GCJ-02** GeoJSON（WGS84 → GCJ-02 转换见 backend/api/geo.py），
前端零计算（架构 §9.6 职责边界表）。

本轮新增：GET /layers/{layer_id}（GeoJSON）、真实 SSE 事件总线（含
Last-Event-ID 断线续传）、feedback 手势回传（draw_polygon / range_drag）。

任务卡 2 增量：图层数据补齐 l_iso（等时圈，透传 reachability 的 GCJ-02 内联几何）与
l_route（路线，WGS84 折点出口转 GCJ-02）；`_poi_coords` / `_grid_coords` 两处历史裸 SQL
已收口进 QueryBuilder（铁律 1：所有 SQL 经编译 + 三层卡点，见 compile_point_query）。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.geo import (
    gcj02_to_wgs84,
    grid_feature_collection,
    line_feature_collection,
    poi_feature_collection,
)
from backend.app import build_services, build_skill_context
from backend.brain.graph import resume_query, run_query
from backend.query_builder import PointQuerySpec
from backend.skills.registry import SkillRegistry
from backend.state.repository import StateNotFoundError, StateRepository

_SSE_IDLE_TIMEOUT_S = 300.0


class CreateSessionIn(BaseModel):
    scene: str = "site_selection"
    slots: dict[str, Any] = Field(default_factory=dict)


class ChatIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    gesture: dict[str, Any] | None = None


class ResumeIn(BaseModel):
    answer: str = Field(min_length=1, max_length=200)


class FeedbackIn(BaseModel):
    """手势 / 约束回传（架构 §9.3）。

    手势几何原样上报（GCJ-02），后端翻译成 WGS84 约束；node_id/action
    为 1.0 版兼容字段（adopt/reject 走 mark_stale 局部重算）。
    """

    type: str = Field(default="reject", description="draw_polygon|point_select|range_drag|constraint_edit|adopt|reject")
    payload: dict[str, Any] = Field(default_factory=dict)
    node_id: str | None = None
    action: str | None = None


class ChatOut(BaseModel):
    session_id: str
    interrupted: bool = False
    question: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    aggs: list[dict[str, Any]] = Field(default_factory=list)
    decision: dict[str, Any] = Field(default_factory=dict)
    map_spec: dict[str, Any] = Field(default_factory=dict)


def create_app() -> FastAPI:
    app = FastAPI(title="GeoMind", version="0.1.0",
                  description="空间智能体内核：同库五模，一条 SQL")
    router = APIRouter(prefix="/api")

    services = build_services()
    repo = StateRepository(services.ob)
    bus = services.events
    SkillRegistry.instance().discover()

    def _ctx(session_id: str) -> Any:
        return build_skill_context(session_id, services)

    # ------------------------------------------------------------------
    # 内部助手（闭包捕获 services，避免模块级全局）
    # ------------------------------------------------------------------
    def _graph_state(sid: str) -> dict[str, Any]:
        """从图 checkpointer 取该 session 最近的 BrainState（图执行态的权威）。"""
        from backend.brain.graph import build_graph

        app = build_graph(_ctx(sid))
        snap = app.get_state({"configurable": {"thread_id": sid}})
        return dict(snap.values or {})

    def _poi_coords(poi_ids: list[int]) -> dict[int, tuple[float, float]]:
        """点查 POI 坐标（WGS84）。四模 SQL 不含坐标列，这里按主键补查。

        铁律 1 收口：SQL 由 QueryBuilder 编译并过三层卡点（此前这里是 `IN (%s,...)` 裸拼）。
        """
        if not poi_ids:
            return {}
        cq = services.query_builder.compile_point_query(
            PointQuerySpec(table="dim_poi", ids=poi_ids))
        rows, _plan = services.query_builder.execute_checked(cq)
        return {int(r["poi_id"]): (float(r["lon"]), float(r["lat"]))
                for r in rows if r.get("lon") is not None}

    def _grid_coords(grid_ids: list[int]) -> dict[int, tuple[float, float]]:
        """点查网格中心点（WGS84，取 workday_evening 时段，与查询默认一致）。"""
        if not grid_ids:
            return {}
        cq = services.query_builder.compile_point_query(PointQuerySpec(
            table="fact_traffic_grid", ids=grid_ids, time_bucket="workday_evening"))
        rows, _plan = services.query_builder.execute_checked(cq)
        return {int(r["grid_id"]): (float(r["center_lon"]), float(r["center_lat"]))
                for r in rows if r.get("center_lon") is not None}

    # ------------------------------------------------------------------
    # 探针与能力清单
    # ------------------------------------------------------------------
    @app.get("/health")
    @app.get("/healthz")
    def health() -> dict[str, Any]:
        return {"ok": services.ob.ping(), "skills": len(SkillRegistry.instance().skills)}

    @router.get("/skills")
    def list_skills() -> dict[str, Any]:
        return {"skills": SkillRegistry.instance().describe_all()}

    # ------------------------------------------------------------------
    # 会话与状态
    # ------------------------------------------------------------------
    @router.post("/sessions")
    def create_session(body: CreateSessionIn) -> dict[str, Any]:
        sid = uuid.uuid4().hex[:16]
        state = repo.create(sid, body.scene, body.slots)
        return {"session_id": sid, "version": state.version}

    @router.get("/sessions/{sid}")
    def get_session(sid: str) -> dict[str, Any]:
        """会话快照：Spatial State + 最近一次图的执行态（架构 §9.3 表格）。"""
        try:
            st = repo.load(sid).model_dump(mode="json")
        except StateNotFoundError as exc:
            raise HTTPException(404, f"会话不存在: {sid}") from exc
        return {"session_id": sid, "state": st, "graph_state": _graph_state(sid)}

    @router.get("/sessions/{sid}/state")
    def get_state(sid: str) -> dict[str, Any]:
        try:
            return repo.load(sid).model_dump(mode="json")
        except StateNotFoundError as exc:
            raise HTTPException(404, f"会话不存在: {sid}") from exc

    # ------------------------------------------------------------------
    # 对话（走 7 步图）+ SSE 埋点
    # ------------------------------------------------------------------
    @router.post("/sessions/{sid}/chat", response_model=ChatOut)
    def chat(sid: str, body: ChatIn) -> ChatOut:
        bus.publish(sid, "message.start", {"query": body.query})
        try:
            r = run_query(body.query, _ctx(sid), gesture=body.gesture, thread_id=sid)
        except Exception as exc:  # noqa: BLE001 - 上报后抛给全局异常处理
            bus.publish(sid, "error", {"message": str(exc)})
            raise
        bus.publish(sid, "message.end", {"interrupted": bool(r.get("interrupted"))})
        return _chat_out(sid, r)

    @router.post("/sessions/{sid}/resume", response_model=ChatOut)
    def resume(sid: str, body: ResumeIn) -> ChatOut:
        bus.publish(sid, "message.start", {"answer": body.answer})
        r = resume_query(body.answer, _ctx(sid), thread_id=sid)
        bus.publish(sid, "message.end", {"interrupted": bool(r.get("interrupted"))})
        return _chat_out(sid, r)

    # ------------------------------------------------------------------
    # 手势 / 反馈回传（架构 §9.3 闭环：手势 → 约束 → 局部重算 → layer.update）
    # ------------------------------------------------------------------
    @router.post("/sessions/{sid}/feedback")
    def feedback(sid: str, body: FeedbackIn) -> dict[str, Any]:
        kind = body.type
        if kind in ("adopt", "reject"):
            try:
                affected = repo.mark_stale(sid, body.node_id or "candidates")
            except StateNotFoundError as exc:
                raise HTTPException(404, f"会话不存在: {sid}") from exc
            bus.publish(sid, "state.delta", {"stale": affected})
            return {"stale": affected, "replan": repo.replan(sid)}

        if kind == "draw_polygon":
            # 框选：GCJ-02 WKT → WGS84 → 以手势重跑一轮（图内 render 会重发 layer.update）
            wkt = body.payload.get("polygon_wkt", "")
            wgs = _wkt_to_wgs84(wkt)
            query = body.payload.get("query", "")
            r = run_query(query, _ctx(sid), gesture={"polygon_wkt": wgs}, thread_id=sid)
            out = _chat_out(sid, r)
            return {"accepted": True, "gesture": "draw_polygon", "result": out.model_dump()}

        if kind == "range_drag":
            center = body.payload.get("center") or []
            if len(center) != 2:
                raise HTTPException(400, "range_drag 需要 payload.center = [lon, lat]（GCJ-02）")
            wlng, wlat = gcj02_to_wgs84(float(center[0]), float(center[1]))
            query = body.payload.get("query", "")
            r = run_query(query, _ctx(sid),
                          gesture={"center": (wlng, wlat),
                                   "radius_m": float(body.payload.get("radius_m", 1500))},
                          thread_id=sid)
            return {"accepted": True, "gesture": "range_drag",
                    "result": _chat_out(sid, r).model_dump()}

        if kind == "point_select":
            poi_id = body.payload.get("poi_id")
            st = _graph_state(sid)
            for c in st.get("candidates", []):
                if c.get("poi_id") == poi_id:
                    return {"accepted": True, "gesture": "point_select", "candidate": c}
            raise HTTPException(404, f"候选中没有 poi_id={poi_id}")

        raise HTTPException(400, f"未知 feedback 类型: {kind}")

    # ------------------------------------------------------------------
    # 图层数据（GeoJSON，GCJ-02）
    # ------------------------------------------------------------------
    @router.get("/sessions/{sid}/layers/{layer_id}")
    def get_layer(sid: str, layer_id: str) -> dict[str, Any]:
        st = _graph_state(sid)
        if layer_id == "l_candidates":
            cands = st.get("candidates") or []
            coords = _poi_coords([c["poi_id"] for c in cands])
            pois = []
            for c in cands:
                xy = coords.get(c["poi_id"])
                if xy is not None:
                    pois.append({**c, "lon": xy[0], "lat": xy[1]})
            return poi_feature_collection(pois)
        if layer_id == "l_heat":
            aggs = st.get("aggs") or []
            coords = _grid_coords([a["grid_id"] for a in aggs])
            grids = []
            for a in aggs:
                xy = coords.get(a["grid_id"])
                if xy is not None:
                    grids.append({**a, "center_lon": xy[0], "center_lat": xy[1]})
            return grid_feature_collection(grids)
        if layer_id == "l_iso":
            return iso_feature_collection(st.get("isochrone"))
        if layer_id == "l_route":
            return route_feature_collection(st.get("route"))
        raise HTTPException(404, f"未知图层: {layer_id}")

    # ------------------------------------------------------------------
    # SSE 事件流（轮询增量 + Last-Event-ID 续传）
    # ------------------------------------------------------------------
    @router.get("/sessions/{sid}/events")
    async def events(sid: str, request: Request) -> Any:
        from fastapi.responses import StreamingResponse

        try:
            last = int(request.headers.get("Last-Event-ID", "0") or 0)
        except ValueError:
            last = 0

        async def gen() -> AsyncIterator[str]:
            nonlocal last
            # 立即发 retry + 注释行：保证首字节即刻下发（否则 EventSource 的
            # open 事件要等首个真实事件才触发，前端会一直显示未连接）；
            # 空闲期每 15s 发心跳注释，防止中间代理把静默长连接掐掉。
            yield "retry: 3000\n: connected\n\n"
            last_beat = time.monotonic()
            deadline = time.monotonic() + _SSE_IDLE_TIMEOUT_S
            while True:
                batch = bus.since(sid, last)
                if batch:
                    for ev in batch:
                        last = ev["seq"]
                        yield f"id: {ev['seq']}\n" + _sse({"type": ev["type"], "data": ev["data"]})
                    deadline = time.monotonic() + _SSE_IDLE_TIMEOUT_S
                    continue
                if time.monotonic() - last_beat > 15.0:
                    yield ": ping\n\n"
                    last_beat = time.monotonic()
                if time.monotonic() > deadline:
                    yield _sse({"type": "timeout"})
                    return
                await asyncio.sleep(0.1)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    app.include_router(router)
    return app


# ---------------------------------------------------------------------------
# 模块级纯助手
# ---------------------------------------------------------------------------
def iso_feature_collection(iso: dict[str, Any] | None) -> dict[str, Any]:
    """等时圈图层数据：透传 reachability 的内联 GeoJSON（已是 GCJ-02 闭合环）。

    架构 §9.6：坐标系转换在后端，reachability 出口时已完成，端点不再二次转换。
    图状态没有产出、或形状不合法 → 空 FeatureCollection（**不 500、不 404**；
    404 只保留给未知 layer id，见 get_layer）。
    """
    fc = (iso or {}).get("geojson")
    if (not isinstance(fc, dict) or fc.get("type") != "FeatureCollection"
            or not isinstance(fc.get("features"), list)):
        return {"type": "FeatureCollection", "features": []}
    return fc


def route_feature_collection(route: dict[str, Any] | None) -> dict[str, Any]:
    """路线图层数据：polyline 是 WGS84（SpatialRouter 出口口径），端点前转 GCJ-02（§9.6）。"""
    if not route:
        return {"type": "FeatureCollection", "features": []}
    pts = [(float(p[0]), float(p[1])) for p in (route.get("polyline") or []) if len(p) == 2]
    props = {k: route.get(k) for k in ("mode", "distance_m", "duration_s")}
    props["point_count"] = len(pts)
    return line_feature_collection(pts, props)


def _wkt_to_wgs84(wkt: str) -> str:
    """POLYGON WKT（GCJ-02）→ WGS84 WKT。非法输入原样返回（后端校验层会拦）。"""
    import re

    m = re.match(r"\s*POLYGON\s*\(\s*\((.*)\)\s*\)\s*$", wkt, re.S)
    if not m:
        return wkt
    pairs = []
    for x, y in re.findall(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)", m.group(1)):
        wlng, wlat = gcj02_to_wgs84(float(x), float(y))
        pairs.append(f"{wlng:.6f} {wlat:.6f}")
    if not pairs:
        return wkt
    return "POLYGON((" + ", ".join(pairs) + "))"


def _chat_out(sid: str, r: dict[str, Any]) -> ChatOut:
    if r.get("interrupted"):
        return ChatOut(session_id=sid, interrupted=True, question=r.get("question"))
    st = r.get("state", {})
    return ChatOut(
        session_id=sid, interrupted=False,
        candidates=st.get("candidates", []), aggs=st.get("aggs", []),
        decision=st.get("decision", {}), map_spec=st.get("map_spec", {}),
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


__all__ = ["ChatIn", "ChatOut", "FeedbackIn", "create_app", "iso_feature_collection",
           "route_feature_collection"]
