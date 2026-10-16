"""L2 决策综合器（架构 §10 `brain/synthesize.py`）。

把 candidates / spatial_join 落下来的特征拼成一段决策上下文，交给 LLM 网关出结论；
网关不可达或报错时**降级为模板结论**，7 步图不因此中断。

任务卡 7 ③ 从 `graph.py` **原样搬出**（函数体一字未改，纯重构）：
`from backend.brain.graph import ...` 的既有路径仍可用（graph.py 里别名导入）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.api.sse_bus import emit
from backend.llm.gateway import LLMMessage, get_gateway
from backend.skills.base import SkillContext

if TYPE_CHECKING:
    from backend.brain.graph import BrainState


def decide_node(state: BrainState, ctx: SkillContext) -> BrainState:
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
