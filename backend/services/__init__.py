"""ServiceBundle：原子 Skill 的统一依赖入口（L4 服务句柄集合）。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ServiceBundle(BaseModel):
    """把 L4 的各服务打成一个包，通过 SkillContext.services 注入。

    避免 Skill 自行 import 全局单例——可测试、可替换（测试可注入 fake）。
    """

    model_config = {"arbitrary_types_allowed": True}

    ob: Any                       # OBConnection
    query_builder: Any            # QueryBuilder
    embedding: Any                # EmbeddingProvider
    amap: Any = None              # AmapGateway（无 key 时降级为库内兜底）
    router: Any = None            # SpatialRouter（L4 计算路由，架构 §2.2：OB 下推 or 高德）
    state_repo: Any = None        # StateRepository（L2 图节点持久化用）
    events: Any = None            # EventBus（SSE 事件总线，backend/api/sse_bus.py）


__all__ = ["ServiceBundle"]
