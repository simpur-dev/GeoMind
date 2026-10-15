"""L5 Spatial State：Pydantic v2 模型（架构 §4）。

字段与架构文档 §4.1 顶层字段表一一对应；JSON 形态与生产库
scene_solution.state 列互通（已由 backend/etl/seed_cd.py 灌入样例）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

StepStatus = Literal["pending", "ready", "done", "stale", "failed"]
LayerKind = Literal["points", "heatmap", "polygon", "line"]


class StepNode(BaseModel):
    """DAG 中的一个计算节点。status=stale 表示其输入已变化、需要重算。"""

    id: str
    skill: str
    status: StepStatus = "pending"
    depends_on: list[str] = Field(default_factory=list)
    result_ref: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    error: str | None = None


class LayerSpec(BaseModel):
    """声明式地图图层（Map Spec 的最小单元）。

    前端只渲染不计算：data_ref 指向 steps 的 result_ref，style 由后端给建议值。
    """

    id: str
    kind: LayerKind
    data_ref: str
    style: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = "ready"
    derived_from: list[str] = Field(default_factory=list)


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


class MapSpec(BaseModel):
    """前端消费的声明式地图规格（架构 §2.2 L1 Map Spec 渲染器）。"""

    version: str = "1.0"
    layers: list[LayerSpec] = Field(default_factory=list)
    viewport: dict[str, Any] | None = None
