"""组装 ServiceBundle：把 L4 服务打成一个包（供 Skill 与 L2 图使用）。

单一构造点，避免散落各处 import 全局单例。
"""
from __future__ import annotations

from backend.api.sse_bus import get_bus
from backend.config import Settings, get_settings
from backend.db.connection import OBConnection
from backend.embeddings import get_provider
from backend.query_builder import QueryBuilder
from backend.services import ServiceBundle
from backend.services.amap_gateway import AmapGateway
from backend.skills.base import SkillContext
from backend.skills.registry import SkillRegistry
from backend.state.repository import StateRepository


def build_services(settings: Settings | None = None) -> ServiceBundle:
    s = settings or get_settings()
    ob = OBConnection(s)
    return ServiceBundle(
        ob=ob,
        query_builder=QueryBuilder(ob, s),
        embedding=get_provider(s),
        amap=AmapGateway(s),
        state_repo=StateRepository(ob),
        events=get_bus(),
    )


def build_skill_context(session_id: str, services: ServiceBundle,
                        state: dict | None = None) -> SkillContext:
    SkillRegistry.instance().discover()
    return SkillContext(session_id=session_id, state=state or {}, services=services,
                        tools=dict(SkillRegistry.instance().skills))


__all__ = ["build_services", "build_skill_context"]
