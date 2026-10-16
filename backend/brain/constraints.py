"""L2 约束建模器（架构 §10 `brain/constraints.py`）。

职责只有一件事：把意图槽位装配成 spec 十键，再叠加 chips 点改的覆盖值
（「时间约束」两类字段口径严格分开：`time_bucket` 是客流时段，
`iso_minutes` / `travel_mode` 是等时圈时间预算与方式）。

任务卡 7 ③ 从 `graph.py` **原样搬出**（函数体一字未改，纯重构）：行为由既有用例钉，
`from backend.brain.graph import _constraints_node` 的既有路径仍然可用（graph.py 里别名导入）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.api.contracts.constraints import ConstraintOverrides
from backend.api.sse_bus import emit
from backend.skills.base import SkillContext

if TYPE_CHECKING:
    from backend.brain.graph import BrainState


def constraints_node(state: BrainState, ctx: SkillContext) -> BrainState:
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
    # chips 点改（卡 6，§9.2）：组装完再覆盖同名键，缺省口径仍由上面这段定。
    # 生效范围取「ConstraintOverrides 声明的键 ∩ 已装配的 spec ∩ 本轮带来的键」：
    # 白名单只有 ConstraintOverrides 一处定义（§9.6 裁决全在后端），绕过端点塞进来的
    # 陌生键（含 center / keywords / time_bucket）也污染不了 spec。无覆盖时整段空转。
    overrides = state.get("spec_overrides") or {}
    for k in overrides.keys() & ConstraintOverrides.model_fields.keys() & spec.keys():
        spec[k] = overrides[k]
    emit(ctx, "constraints.updated", spec)
    return {"spec": spec}
