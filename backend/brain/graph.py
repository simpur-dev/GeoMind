"""L2 LangGraph 7 步状态图（架构 §2.2 L2 / §3.2 数据流）。

图结构固定为 7 节点（可审计、不漂移、省 token），只有 intent 节点可
追问（interrupt，对标 MCP Elicitation）；其余节点纯确定性，不调 LLM。

    intent → constraints → candidates → spatial_join → decide → render → feedback

checkpointer：按 GEOMIND_CHECKPOINT_BACKEND 选后端（架构 §2.2 L5）——
`memory` 进程内（开发/测试默认），`ob` 同库 SeekDB 持久化（重启进程后会话与
interrupt 态都在，见 backend/db/checkpoint_ob.py）。业务态（Spatial State）
在 OB scene_solution 表，由图节点通过 StateRepository 读写。

services 通过 build_graph(ctx) 的闭包注入——不用模块级全局（多会话会串，
本项目实测踩过）。
"""
from __future__ import annotations

from typing import Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.api.contracts.map_spec import LayerSource, LayerSpec
from backend.api.sse_bus import emit
from backend.brain.intent import IntentResult, make_intent_parser
from backend.config import get_settings
from backend.db.checkpoint_ob import make_checkpointer
from backend.db.connection import OBConnection
from backend.llm.gateway import LLMMessage, get_gateway
from backend.skills.base import SkillContext
from backend.skills.registry import SkillRegistry
from backend.state.models import Evidence, SpatialState, StepNode
from backend.state.repository import StateRepository

GRAPH_NODES = ["intent", "constraints", "candidates", "spatial_join", "decide", "render", "feedback"]


def _new_checkpointer() -> Any:
    """按配置开关选检查点后端（架构 §2.2 L5）：memory 进程内 / ob 同库持久化。

    OBConnection 构造是惰性的（首次执行才取连接），所以 import 期建它不要求库在线。
    """
    s = get_settings()
    return (make_checkpointer("memory") if s.checkpoint_backend == "memory"
            else make_checkpointer("ob", OBConnection(s)))


# checkpointer 必须跨调用共享且**只构造一次**：
#   · 每次 build_graph 若新建，resume 时拿到的是空 checkpoint（实测踩坑：KeyError 'query'）；
#   · 节点与路由层一律不感知后端类型（换 ob/memory 只改 GEOMIND_CHECKPOINT_BACKEND）。
_CHECKPOINTER = _new_checkpointer()


class BrainState(TypedDict, total=False):
    """图状态（与 Spatial State 分离：图执行态在内存，业务态在 OB）。"""

    session_id: str
    query: str
    gesture: dict[str, Any] | None
    intent: dict[str, Any] | None
    spec: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    aggs: list[dict[str, Any]]
    isochrone: dict[str, Any]      # reachability 产出（含内联 GCJ-02 geojson 与 render_spec）
    iso_filter_skip: str           # 等时圈没参与候选筛选的原因（gesture_polygon | no_polygon）
    decision: dict[str, Any]
    map_spec: dict[str, Any]
    feedback: dict[str, Any] | None
    error: str | None


# ---------------------------------------------------------------------------
# 纯节点（不依赖 services，可独立测试）
# ---------------------------------------------------------------------------
def _intent_node(state: BrainState, ctx: SkillContext) -> BrainState:
    # 解析器按 GEOMIND_INTENT_BACKEND 切换（rule 默认 | llm），同 IntentResult 契约；
    # LLM 版任何失败在解析器内部降级为规则版，节点不感知
    parser = make_intent_parser()
    result: IntentResult = parser.parse(state.get("query", ""), state.get("gesture"))
    if result.question:
        # HITL：中断并等待用户补充（恢复时经 Command(resume=...) 传入）
        emit(ctx, "interrupt.required", {"question": result.question})
        answer = interrupt({"question": result.question, "slots": result.slots,
                            "confidence": result.confidence})
        if isinstance(answer, str) and answer.strip():
            merged = state["query"] + " " + answer.strip()
            result = parser.parse(merged, state.get("gesture"))
    emit(ctx, "intent.updated", {"slots": result.slots, "scene": result.scene,
                                 "confidence": result.confidence})
    return {"intent": result.to_dict()}


def _constraints_node(state: BrainState, ctx: SkillContext) -> BrainState:
    slots = (state.get("intent") or {}).get("slots", {})
    spec = {
        "center": tuple(slots["center"]) if slots.get("center") else None,
        "radius_m": slots.get("radius_m"),
        "polygon_wkt": slots.get("polygon_wkt"),
        "category_id": slots.get("category_id"),
        "min_rating": 4.0,
        "keywords": slots.get("keywords", []),
        "query_text": slots.get("query_text"),
        "time_bucket": "workday_evening",
        "limit": 10,
        # 「时间约束」两类字段口径严格分开（架构 §2.2 L2 约束建模器）：
        #   time_bucket = 客流时段（workday_evening 等，下推 fact_traffic_grid）
        #   iso_minutes / travel_mode = 等时圈时间预算与出行方式（下发 reachability）
        # 这两个键由意图器填（规则/LLM 同契约，卡 4），缺省即 None=用户没提；
        # candidates 节点据其有无决定「反哺检索」还是「仅展示」（卡 5，架构 §7）。
        # chips 点改是第三个来源（卡 6）。
        "iso_minutes": slots.get("iso_minutes"),
        "travel_mode": slots.get("travel_mode"),
    }
    emit(ctx, "constraints.updated", spec)
    return {"spec": spec}


def _decide_node(state: BrainState, ctx: SkillContext) -> BrainState:
    llm = get_gateway()
    aggs = state.get("aggs") or []
    cands = state.get("candidates") or []
    if not aggs:
        return {"decision": {"summary": "该范围内没有可用网格，建议扩大范围或更换类目。"}}
    top = max(aggs, key=lambda a: (a.get("flow") or 0))
    feats = (f"候选 POI {len(cands)} 个；客流最高网格 {top['grid_id']}"
             f"（flow={top.get('flow')}，POI {top.get('poi_cnt')} 个，"
             f"评分 {top.get('avg_rating')}）")
    try:
        prompt = ("你是连锁门店选址分析师。基于以下决策特征，给出一句选址结论与理由："
                  "直接说推荐哪个网格、为什么，不超过 80 字，不要罗列选项或反问。\n"
                  f"FEATURES: {feats}")
        text = llm.chat([LLMMessage(role="user", content=prompt)], temperature=0.0)
    except Exception as exc:  # noqa: BLE001 - LLM 不可达/报错时降级为模板结论，7 步图不中断
        text = (f"结论：基于决策特征给出可解释建议 —— {feats}。"
                f"（真实 LLM 网关暂不可用，已降级为模板结论：{exc}）")
    decision = {"summary": text, "top_grid": top}
    emit(ctx, "decision.updated", decision)
    return {"decision": decision}


# ---------------------------------------------------------------------------
# 等时圈编排助手（架构 §7「高德出多边形 → OB ST_Contains 批量过滤」/ §3.1 闭环）
#
# 放在模块级而不是 build_graph 闭包里：单测能直接打这三个纯函数，
# 不必为了验证一条判据把整张图跑一遍。
# ---------------------------------------------------------------------------
_ISO_CENTER_TOL = 1e-6      # 中心点复用判据的容差（约 0.1 m，比坐标本身更严）


def _iso_defaults() -> tuple[int, str]:
    """等时圈的两个缺省值从 Skill Schema 读，图里不写第二份（免得两处漂移）。"""
    from backend.skills.atomic.reachability import ReachabilityInput

    f = ReachabilityInput.model_fields
    return int(f["minutes"].default), str(f["mode"].default)


def _iso_request(spec: dict[str, Any]) -> dict[str, Any] | None:
    """本轮要算的等时圈（中心 + 时间预算 + 方式）；没有中心点就不算。

    `explicit` 只回答一个问题：用户有没有**显式**给时间预算。给了才反哺候选检索
    （架构 §3.1：约束 → 空间计算 → 检索），没给就只是默认展示圈。
    """
    center = spec.get("center")
    if not center:
        return None
    d_minutes, d_mode = _iso_defaults()
    minutes = spec.get("iso_minutes")
    mode = spec.get("travel_mode")
    return {
        "center": (float(center[0]), float(center[1])),
        "minutes": int(minutes) if minutes else d_minutes,
        "mode": str(mode) if mode else d_mode,
        "explicit": bool(minutes),
    }


def _iso_matches(existing: Any, req: dict[str, Any]) -> bool:
    """去重判据：mode / minutes / center 三项一致才复用（复用即**不再调高德**）。"""
    if not isinstance(existing, dict) or not existing:
        return False
    c = existing.get("center") or {}
    try:
        got = (float(c["lon"]), float(c["lat"]))
    except (KeyError, TypeError, ValueError):
        return False
    if any(abs(a - b) > _ISO_CENTER_TOL for a, b in zip(got, req["center"], strict=True)):
        return False
    if existing.get("mode") != req["mode"]:
        return False
    got_minutes = existing.get("minutes")
    if got_minutes is None:
        return False
    try:
        return float(got_minutes) == float(req["minutes"])
    except (TypeError, ValueError):
        return False


def _isochrone_update(state: BrainState, ctx: SkillContext) -> dict[str, Any]:
    """产出（或复用）等时圈并落图状态（架构 §2.3 第 4 步「空间计算」）。

    只给既有节点补一次 L4 调用：节点集合与边一律不动（§5.6 冻结的是主图骨架）。
    几何计算全在 SpatialRouter，本节点不知道「方位角/半径」为何物（铁律 2）。
    """
    req = _iso_request(state.get("spec") or {})
    if req is None:
        return {}
    if _iso_matches(state.get("isochrone"), req):
        return {}           # 复用：一次高德请求都不发

    from backend.services.spatial_router import PlaceRef
    from backend.skills.atomic.reachability import ReachabilityInput

    emit(ctx, "tool.start", {"skill": "reachability"})
    iso = ctx.tools["reachability"].run(ReachabilityInput(
        center=PlaceRef(lonlat=req["center"]), minutes=req["minutes"], mode=req["mode"]), ctx)
    emit(ctx, "tool.end", {"skill": "reachability", "elapsed_ms": iso.elapsed_ms,
                            "degraded": iso.degraded})
    # 失败也落一个空产出（而不是抛）：图不中断，l_iso 端点据此返回空集
    return {"isochrone": iso.model_dump(mode="json") if iso.ok else {}}


# ---------------------------------------------------------------------------
# 图装配（services 经闭包注入）
# ---------------------------------------------------------------------------
def build_graph(ctx: SkillContext) -> Any:
    """装配 7 步图。ctx.services 承载 L4 服务（ob / query_builder / embedding / state_repo）。"""

    def candidates(state: BrainState, ctx: SkillContext) -> BrainState:
        from backend.skills.atomic.multi_modal_search import MultiModalSearchInput

        spec = state.get("spec") or {}
        update: dict[str, Any] = {}

        # 用户显式给了时间预算 → 先算等时圈，再拿多边形反哺候选检索（§7 闭环）。
        # 没给就完全维持现状：一个字节的行为都不变，等时圈留到 spatial_join 只做展示。
        iso_poly: str | None = None
        req = _iso_request(spec)
        if req is not None and req["explicit"]:
            update = _isochrone_update(state, ctx)
            iso = update.get("isochrone") or {}
            if spec.get("polygon_wkt"):
                # 用户亲手画的框优先于推算的可达面（两个圆/面谓词同开会互相打架）
                update["iso_filter_skip"] = "gesture_polygon"
            elif iso.get("polygon_wkt"):
                iso_poly = str(iso["polygon_wkt"])
            else:
                update["iso_filter_skip"] = "no_polygon"   # 检索宁可宽不可空

        emit(ctx, "tool.start", {"skill": "multi_modal_search"})
        out = ctx.tools["multi_modal_search"].run(MultiModalSearchInput(
            # 反哺时按 SpatialConstraint 的二选一契约让位：面约束进，圆约束出
            center=None if iso_poly else spec.get("center"),
            radius_m=None if iso_poly else spec.get("radius_m"),
            polygon_wkt=iso_poly or spec.get("polygon_wkt"),
            category_id=spec.get("category_id"),
            min_rating=spec.get("min_rating"), keywords=spec.get("keywords") or [],
            query_text=spec.get("query_text"), limit=spec.get("limit") or 10,
        ), ctx)
        emit(ctx, "tool.end", {"skill": "multi_modal_search", "elapsed_ms": out.elapsed_ms,
                                 "degraded": out.degraded,
                                 **({"iso_filter_skip": update["iso_filter_skip"]}
                                    if update.get("iso_filter_skip") else {})})
        # mode="json"：rating/avg_rating 是 Decimal，必须在此转成 float——
        # 这些 dict 会进 SSE 事件（decision.updated 的 top_grid 等），
        # 生成器里 json.dumps 不认 Decimal，会把整条事件流打断（实测踩坑）。
        return cast(BrainState,
                    {"candidates": [c.model_dump(mode="json") for c in out.candidates], **update})

    def spatial_join(state: BrainState, ctx: SkillContext) -> BrainState:
        from backend.skills.atomic.spatial_join_agg import SpatialJoinAggInput

        spec = state.get("spec") or {}
        emit(ctx, "tool.start", {"skill": "spatial_join_agg"})
        out = ctx.tools["spatial_join_agg"].run(SpatialJoinAggInput(
            center=spec.get("center"), radius_m=spec.get("radius_m"),
            time_bucket=spec.get("time_bucket") or "workday_evening",
            category_id=spec.get("category_id"), limit=100,
        ), ctx)
        emit(ctx, "tool.end", {"skill": "spatial_join_agg", "elapsed_ms": out.elapsed_ms})
        return cast(BrainState,
                    {"aggs": [a.model_dump(mode="json") for a in out.aggs],
                     **_isochrone_update(state, ctx)})

    def render(state: BrainState, ctx: SkillContext) -> BrainState:
        scene = ctx.tools.get("site_selection")
        if scene is not None:
            spec = scene.render_spec(ctx, state.get("session_id", ""), state)
        else:
            spec = {"version": "1.0", "layers": [], "map_view": None}
        emit(ctx, "layer.update", {"layers": [ly.get("id") for ly in spec.get("layers", [])],
                                     "stale": [ly.get("id") for ly in spec.get("layers", [])
                                               if ly.get("status") == "stale"]})
        if spec.get("map_view"):
            emit(ctx, "map.view", spec["map_view"])
        return {"map_spec": spec}

    def feedback(state: BrainState, ctx: SkillContext) -> BrainState:
        """反馈回流：查询写入 knowledge_doc（长期记忆，架构 §3.2）+ 状态落库。"""
        ob = ctx.services.ob
        query = state.get("query", "")
        if query:
            emb = ctx.services.embedding.embed_doc(f"用户查询：{query}")
            ob.execute(
                "INSERT INTO knowledge_doc (doc_id, title, text, meta, emb) VALUES "
                "(%(id)s, %(title)s, %(text)s, %(meta)s, %(emb)s) "
                "ON DUPLICATE KEY UPDATE text = VALUES(text), emb = VALUES(emb)",
                {"id": 900000 + (hash(query) % 100000), "title": "会话查询回流",
                 "text": f"用户查询：{query}", "meta": '{"source":"session_feedback"}',
                 "emb": emb})
        repo: StateRepository | None = getattr(ctx.services, "state_repo", None)
        if repo is not None:
            try:
                st = state_to_spatial_state(state["session_id"], "site_selection", state)
                repo.save(st)
                emit(ctx, "state.delta", {"version": st.version, "scene": st.scene})
            except Exception:
                pass
        return {"feedback": {"written": bool(query)}}

    def _bind(fn: Any) -> Any:
        """把 (state, ctx) 节点适配成 StateGraph 的 (state) 签名。"""
        def node(state: BrainState) -> BrainState:
            return fn(state, ctx)
        return node

    g: StateGraph = StateGraph(BrainState)
    g.add_node("intent", _bind(_intent_node))
    g.add_node("constraints", _bind(_constraints_node))
    g.add_node("candidates", _bind(candidates))
    g.add_node("spatial_join", _bind(spatial_join))
    g.add_node("decide", _bind(_decide_node))
    g.add_node("render", _bind(render))
    g.add_node("feedback", _bind(feedback))
    for a, b in zip(GRAPH_NODES, GRAPH_NODES[1:], strict=False):
        g.add_edge(a, b)
    g.add_edge(GRAPH_NODES[-1], END)
    g.add_edge(START, GRAPH_NODES[0])
    # checkpointer 是 interrupt/Command(resume=...) 的前提（langgraph 1.x 强制）
    return g.compile(checkpointer=_CHECKPOINTER)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def _unwrap(result: dict[str, Any], thread_id: str) -> dict[str, Any]:
    if "__interrupt__" in result:
        iv = result["__interrupt__"]
        return {"interrupted": True, "thread_id": thread_id,
                "question": iv[0].value if iv else None}
    return {"interrupted": False,
            "state": {k: v for k, v in result.items() if not k.startswith("_")}}


def run_query(query: str, ctx: SkillContext, *, gesture: dict[str, Any] | None = None,
              thread_id: str = "demo") -> dict[str, Any]:
    """跑一轮 7 步；intent 触发中断时返回中断信息（含 thread_id 供恢复）。"""
    app = build_graph(ctx)
    config = {"configurable": {"thread_id": thread_id}}
    result = app.invoke({"session_id": ctx.session_id, "query": query, "gesture": gesture}, config)
    return _unwrap(result, thread_id)


def resume_query(answer: str, ctx: SkillContext, *, thread_id: str = "demo") -> dict[str, Any]:
    """用户回答追问后恢复执行。"""
    from langgraph.types import Command

    app = build_graph(ctx)
    config = {"configurable": {"thread_id": thread_id}}
    result = app.invoke(Command(resume=answer), config)
    return _unwrap(result, thread_id)


def state_to_spatial_state(session_id: str, scene: str, brain: Any) -> SpatialState:
    """把图状态落成 Spatial State（供 StateRepository 持久化）。"""
    return SpatialState(
        session_id=session_id, scene=scene, version=1,
        slots=(brain.get("intent") or {}).get("slots", {}),
        steps=[StepNode(id=n, skill=n, status="done") for n in GRAPH_NODES],
        layers=[LayerSpec(id="l_candidates", kind="poi", data_ref="candidates",
                          source=LayerSource(href="/api/sessions/x/layers/l_candidates")),
                LayerSpec(id="l_heat", kind="heatmap", data_ref="fact_traffic_grid",
                          source=LayerSource(href="/api/sessions/x/layers/l_heat"))],
        evidence=[Evidence(type="sql", ref="multi_modal_search", note="四模混合检索")],
    )


__all__ = ["GRAPH_NODES", "BrainState", "build_graph", "run_query", "resume_query",
           "state_to_spatial_state", "SkillRegistry"]
