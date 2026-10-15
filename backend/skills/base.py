"""L3 Skill 层基类（架构 §5.2，签名与架构文档一致，不自行改动）。

铁律落地：
  · run() 内禁止坐标几何计算 —— 只能下发 SQL（QueryBuilder）或走高德网关；
  · 场景 Skill 只写编排 + 评分口径 + 渲染模板，数据访问全在 L4。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

InT = TypeVar("InT", bound=BaseModel)
OutT = TypeVar("OutT", bound=BaseModel)


class ToolContext(BaseModel):
    """一次原子工具调用的运行上下文：DB 连接、高德网关、embedding 服务。"""

    model_config = {"arbitrary_types_allowed": True}

    ob_dsn: str = ""
    amap_key: str = ""
    amap_cache_ttl: int = 86400
    embed_endpoint: str = ""
    ob: Any = None          # OBConnection（延迟注入）
    query_builder: Any = None  # QueryBuilder
    embedding: Any = None   # EmbeddingProvider
    amap: Any = None        # AmapGateway（无 key 时降级）


class SkillContext(BaseModel):
    """场景 Skill 执行上下文：当前会话的 Spatial State 快照 + 工具句柄。

    services：ServiceBundle（ob / query_builder / embedding / amap）。
    架构 §5.2 原文只有 db 与 amap 两个句柄；这里新增 services 作为统一入口
    （db/amap 保留兼容），避免每个原子 Skill 各自拼装依赖。
    """

    model_config = {"arbitrary_types_allowed": True}

    session_id: str
    state: dict[str, Any] = Field(default_factory=dict)   # SpatialState 的 dict 形态
    tools: dict[str, Any] = Field(default_factory=dict)   # name -> 已注册原子 Skill 实例
    db: Any = None
    amap: Any = None
    services: Any = None                                  # ServiceBundle


class SkillMetadata(BaseModel):
    name: str
    version: str = "1.0.0"
    scene: str = "atomic"           # atomic | site_selection | footfall ...
    description: str
    intent_examples: list[str] = Field(default_factory=list)  # 用于场景路由


class SpatialSkill(ABC, Generic[InT, OutT]):
    """所有 Skill 的统一基类。场景 Skill 与原子 Skill 共用（架构 §5.2 原样）。"""

    metadata: SkillMetadata
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    required_tools: list[str] = []   # 声明依赖的原子 Skill 名

    @abstractmethod
    def build_plan(self, ctx: SkillContext) -> list[dict]:
        """场景 Skill：返回可执行 DAG 模板，节点引用原子工具名。
        原子 Skill 可返回 [{"id": self.metadata.name, "skill": self.metadata.name}]。"""

    @abstractmethod
    def run(self, inputs: InT, ctx: SkillContext) -> OutT:
        """执行：原子 Skill 在此下推 SQL / 调高德；场景 Skill 在此编排。
        禁止在此做坐标几何计算。"""

    def synthesize(self, ctx: SkillContext) -> dict:
        """决策综合：LLM 仅消费决策特征，输出可解释结论。默认实现可被子类覆盖。"""
        raise NotImplementedError

    def render_spec(self, ctx: SkillContext, session_id: str = "",
                    state: dict[str, Any] | None = None) -> dict:
        """把状态结果转成声明式 Map Spec 图层 JSON（架构 §9.3）。

        session_id 供 source.href 回指本会话的 GeoJSON 端点；state 是图执行态
        （取 spec.center 等渲染镜头参数）。默认参数保持与旧签名兼容。
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 通用工具
    # ------------------------------------------------------------------
    def describe(self) -> dict:
        """MCP tools/list 用的描述（Pydantic schema 自动导出 JSON Schema）。"""
        return {
            "name": self.metadata.name,
            "description": self.metadata.description,
            "scene": self.metadata.scene,
            "version": self.metadata.version,
            "inputSchema": self.input_schema.model_json_schema(),
        }
