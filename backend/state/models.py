"""L5 Spatial State：Pydantic v2 模型（架构 §4）。

字段与架构文档 §4.1 顶层字段表一一对应；JSON 形态与生产库
scene_solution.state 列互通（已由 backend/etl/seed_cd.py 灌入样例）。

Map Spec 相关模型（LayerSpec / MapSpec 等）单一模型源在
backend/api/contracts/map_spec.py（架构 §9.3 前端契约），此处 re-export，
避免持久化态与前端契约两套定义漂移。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from backend.api.contracts.map_spec import (
    FeedbackEvent,
    LayerKind,
    LayerSource,
    LayerSpec,
    LayerStatus,
    MapSpec,
    MapView,
    PanelKind,
    PanelSpec,
)

StepStatus = Literal["pending", "ready", "done", "stale", "failed"]


class StepNode(BaseModel):
    """DAG 中的一个计算节点。status=stale 表示其输入已变化、需要重算。"""

    id: str
    skill: str
    status: StepStatus = "pending"
    depends_on: list[str] = Field(default_factory=list)
    result_ref: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    error: str | None = None


class Evidence(BaseModel):
    """可解释性证据：一条结论背后的 SQL / 数据引用。"""

    type: Literal["sql", "data", "amap", "llm"]
    ref: str
    note: str = ""


class SpatialState(BaseModel):
    """会话的结构化空间状态（架构 §4.1）。"""

    session_id: str
    scene: str
    version: int = 1
    slots: dict[str, Any] = Field(default_factory=dict)
    steps: list[StepNode] = Field(default_factory=list)
    layers: list[LayerSpec] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=datetime.now)

    # ------------------------------------------------------------------
    # 依赖图操作（架构 §4.3 局部重算机制）
    # ------------------------------------------------------------------
    def step_index(self) -> dict[str, StepNode]:
        return {s.id: s for s in self.steps}

    def mark_stale(self, node_id: str) -> list[str]:
        """把 node_id 及其**反向依赖闭包**标为 stale，返回受影响节点 id 列表。

        反向依赖：任何 depends_on/derived_from 直接或间接引用受影响节点的步骤。
        """
        idx = self.step_index()
        if node_id not in idx:
            raise KeyError(f"节点不存在: {node_id}")
        affected = {node_id}
        changed = True
        while changed:
            changed = False
            for s in self.steps:
                if s.id in affected:
                    continue
                if affected & (set(s.depends_on) | set(s.derived_from)):
                    affected.add(s.id)
                    changed = True
        for sid in affected:
            idx[sid].status = "stale"
        return sorted(affected)

    def replan(self) -> list[str]:
        """拓扑序返回所有 stale 节点（依赖先行）。"""
        idx = self.step_index()
        stale = {s.id for s in self.steps if s.status == "stale"}
        if not stale:
            return []
        ordered: list[str] = []
        done: set[str] = set()
        while stale - done:
            progressed = False
            for sid in sorted(stale - done):
                deps = set(idx[sid].depends_on) | set(idx[sid].derived_from)
                # 只等待同样需要重算的依赖；非 stale 依赖视为已就绪
                pending_deps = deps & (stale - done) - {sid}
                if not pending_deps:
                    ordered.append(sid)
                    done.add(sid)
                    progressed = True
            if not progressed:  # 依赖成环，防御性退出
                ordered.extend(sorted(stale - done))
                break
        return ordered

    def touch(self) -> None:
        self.version += 1
        self.updated_at = datetime.now()


__all__ = [
    "Evidence", "FeedbackEvent", "LayerKind", "LayerSource", "LayerSpec",
    "LayerStatus", "MapSpec", "MapView", "PanelKind", "PanelSpec",
    "SpatialState", "StepNode", "StepStatus",
]
