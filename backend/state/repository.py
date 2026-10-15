"""L5 状态仓库：Spatial State 的 OB 持久化（架构 §2.2 L5 / §4）。

表：scene_solution（JSON 列 + version）。状态权威在 OB，图执行态在
LangGraph checkpointer（内存）——两者分工见架构 §2.2 L5。
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from backend.db.connection import OBConnection
from backend.state.models import Evidence, SpatialState


class StateNotFoundError(KeyError):
    """会话不存在。"""


class StateRepository:
    """Spatial State 读写 + 依赖图局部重算标记。"""

    def __init__(self, ob: OBConnection) -> None:
        self.ob = ob

    # ------------------------------------------------------------------
    def load(self, session_id: str) -> SpatialState:
        rows = self.ob.execute(
            "SELECT session_id, scene, version, state, created_at, updated_at "
            "FROM scene_solution WHERE session_id = %(sid)s", {"sid": session_id})
        if not rows:
            raise StateNotFoundError(session_id)
        r = rows[0]
        try:
            state_dict = json.loads(r["state"]) if isinstance(r["state"], str) else dict(r["state"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"scene_solution.state 不是合法 JSON: {exc}") from exc
        try:
            state = SpatialState(**state_dict)
        except ValidationError as exc:
            raise ValueError(f"Spatial State 校验失败: {exc}") from exc
        # 表的 version 列是权威（DB 侧 ON DUPLICATE KEY UPDATE version=version+1）。
        # JSON 内的 version 只是快照，若不同步会读到旧值（本项目实测踩坑：save 后 version 不回涨）。
        state.version = int(r["version"])
        return state

    def exists(self, session_id: str) -> bool:
        rows = self.ob.execute(
            "SELECT 1 AS ok FROM scene_solution WHERE session_id = %(sid)s LIMIT 1",
            {"sid": session_id})
        return bool(rows)

    def save(self, state: SpatialState) -> SpatialState:
        """写回并自增 version（DB 侧 ON DUPLICATE KEY UPDATE version=version+1）。"""
        self.ob.execute(
            "INSERT INTO scene_solution (session_id, scene, version, state, created_at, updated_at) "
            "VALUES (%(sid)s, %(scene)s, %(version)s, %(state)s, NOW(), NOW()) "
            "ON DUPLICATE KEY UPDATE version = version + 1, state = VALUES(state), updated_at = NOW()",
            {"sid": state.session_id, "scene": state.scene, "version": state.version,
             "state": state.model_dump_json()})
        return self.load(state.session_id)

    def create(self, session_id: str, scene: str, slots: dict[str, Any] | None = None) -> SpatialState:
        state = SpatialState(session_id=session_id, scene=scene, version=1,
                             slots=slots or {}, steps=[], layers=[], evidence=[])
        return self.save(state)

    # ------------------------------------------------------------------
    def mark_stale(self, session_id: str, node_id: str) -> list[str]:
        """标 stale 并持久化，返回受影响节点 id 列表（架构 §4.3）。"""
        state = self.load(session_id)
        affected = state.mark_stale(node_id)
        self.save(state)
        return affected

    def replan(self, session_id: str) -> list[str]:
        """拓扑序返回待重算节点。"""
        return self.load(session_id).replan()

    # ------------------------------------------------------------------
    def append_evidence(self, session_id: str, evidence: Evidence | dict[str, Any]) -> None:
        state = self.load(session_id)
        ev = evidence if isinstance(evidence, Evidence) else Evidence(**evidence)
        state.evidence.append(ev)
        self.save(state)
