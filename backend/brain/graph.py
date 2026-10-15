"""L2 LangGraph 7 步状态图（架构 §2.2 L2 / §3.2 数据流）。

图结构固定为 7 节点（可审计、不漂移、省 token），只有 intent 节点可
追问（interrupt，对标 MCP Elicitation）；其余节点纯确定性，不调 LLM。

    intent → constraints → candidates → spatial_join → decide → render → feedback

checkpointer：MemorySaver（阶段 0）。业务态（Spatial State）在 OB
scene_solution 表，由图节点通过 StateRepository 读写。

services 通过 build_graph(ctx) 的闭包注入——不用模块级全局（多会话会串，
本项目实测踩过）。
"""
from __future__ import annotations

from typing import Any, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.brain.intent import IntentResult, RuleIntentParser
from backend.llm.gateway import LLMMessage, get_gateway
from backend.skills.base import SkillContext
from backend.skills.registry import SkillRegistry
from backend.state.models import Evidence, LayerSpec, SpatialState, StepNode
from backend.state.repository import StateRepository

GRAPH_NODES = ["intent", "constraints", "candidates", "spatial_join", "decide", "render", "feedback"]

# checkpointer 必须跨调用共享：每次 build_graph 若新建 MemorySaver，
# resume 时拿到的是空 checkpoint（本项目实测踩坑：KeyError 'query'）。
_CHECKPOINTER = MemorySaver()


class BrainState(TypedDict, total=False):
    """图状态（与 Spatial State 分离：图执行态在内存，业务态在 OB）。"""

    session_id: str
    query: str
    gesture: dict[str, Any] | None
    intent: dict[str, Any] | None
    spec: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    aggs: list[dict[str, Any]]
    decision: dict[str, Any]
    map_spec: dict[str, Any]
    feedback: dict[str, Any] | None
    error: str | None


# ---------------------------------------------------------------------------
# 纯节点（不依赖 services，可独立测试）
# ---------------------------------------------------------------------------
def _intent_node(state: BrainState) -> BrainState:
    parser = RuleIntentParser()
    result: IntentResult = parser.parse(state.get("query", ""), state.get("gesture"))
    if result.question:
        # HITL：中断并等待用户补充（恢复时经 Command(resume=...) 传入）
        answer = interrupt({"question": result.question, "slots": result.slots,
                            "confidence": result.confidence})
        if isinstance(answer, str) and answer.strip():
            merged = state["query"] + " " + answer.strip()
            result = parser.parse(merged, state.get("gesture"))
    return {"intent": result.to_dict()}


def _constraints_node(state: BrainState) -> BrainState:
    slots = (state.get("intent") or {}).get("slots", {})
    return {"spec": {
        "center": tuple(slots["center"]) if slots.get("center") else None,
        "radius_m": slots.get("radius_m"),
        "polygon_wkt": slots.get("polygon_wkt"),
        "category_id": slots.get("category_id"),
        "min_rating": 4.0,
        "keywords": slots.get("keywords", []),
        "query_text": slots.get("query_text"),
        "time_bucket": "workday_evening",
        "limit": 10,
    }}


def _decide_node(state: BrainState) -> BrainState:
    llm = get_gateway()
    aggs = state.get("aggs") or []
    cands = state.get("candidates") or []
    if not aggs:
        return {"decision": {"summary": "该范围内没有可用网格，建议扩大范围或更换类目。"}}
    top = max(aggs, key=lambda a: (a.get("flow") or 0))
    feats = (f"候选 POI {len(cands)} 个；客流最高网格 {top['grid_id']}"
             f"（flow={top.get('flow')}，POI {top.get('poi_cnt')} 个，"
             f"评分 {top.get('avg_rating')}）")
    text = llm.chat([LLMMessage(role="user", content=f"FEATURES: {feats}")], temperature=0.0)
    return {"decision": {"summary": text, "top_grid": top}}


# ---------------------------------------------------------------------------
# 图装配（services 经闭包注入）
# ---------------------------------------------------------------------------
def build_graph(ctx: SkillContext) -> Any:
    """装配 7 步图。ctx.services 承载 L4 服务（ob / query_builder / embedding / state_repo）。"""

    def candidates(state: BrainState) -> BrainState:
        from backend.skills.atomic.multi_modal_search import MultiModalSearchInput

        spec = state.get("spec") or {}
        out = ctx.tools["multi_modal_search"].run(MultiModalSearchInput(
            center=spec.get("center"), radius_m=spec.get("radius_m"),
            polygon_wkt=spec.get("polygon_wkt"), category_id=spec.get("category_id"),
            min_rating=spec.get("min_rating"), keywords=spec.get("keywords") or [],
            query_text=spec.get("query_text"), limit=spec.get("limit") or 10,
        ), ctx)
        return {"candidates": [c.model_dump() for c in out.candidates]}

    def spatial_join(state: BrainState) -> BrainState:
        from backend.skills.atomic.spatial_join_agg import SpatialJoinAggInput

        spec = state.get("spec") or {}
        out = ctx.tools["spatial_join_agg"].run(SpatialJoinAggInput(
            center=spec.get("center"), radius_m=spec.get("radius_m"),
            time_bucket=spec.get("time_bucket") or "workday_evening",
            category_id=spec.get("category_id"), limit=100,
        ), ctx)
        return {"aggs": [a.model_dump() for a in out.aggs]}

    def render(state: BrainState) -> BrainState:
        scene = ctx.tools.get("site_selection")
        spec = scene.render_spec(ctx) if scene is not None else {"version": "1.0", "layers": []}
        return {"map_spec": spec}

    def feedback(state: BrainState) -> BrainState:
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
            except Exception:
                pass
        return {"feedback": {"written": bool(query)}}

    g: StateGraph = StateGraph(BrainState)
    g.add_node("intent", _intent_node)
    g.add_node("constraints", _constraints_node)
    g.add_node("candidates", candidates)
    g.add_node("spatial_join", spatial_join)
    g.add_node("decide", _decide_node)
    g.add_node("render", render)
    g.add_node("feedback", feedback)
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
        layers=[LayerSpec(id="l_candidates", kind="points", data_ref="candidates"),
                LayerSpec(id="l_heat", kind="heatmap", data_ref="fact_traffic_grid")],
        evidence=[Evidence(type="sql", ref="multi_modal_search", note="四模混合检索")],
    )


__all__ = ["GRAPH_NODES", "BrainState", "build_graph", "run_query", "resume_query",
           "state_to_spatial_state", "SkillRegistry"]
