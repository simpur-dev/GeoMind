"""原子 Skill：知识库混合检索（长期记忆，复用同一套多模检索）。

架构 §2.2 L5：领域知识、用户偏好、历史方案向量化后仍存 OceanBase
knowledge_doc（VECTOR 列），不引第二套向量库。
"""
from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from backend.query_builder import (
    HybridQuerySpec,
    ScalarConstraint,
    SemanticConstraint,
    SpatialConstraint,
)
from backend.skills.base import SkillContext, SkillMetadata, SpatialSkill
from backend.skills.registry import register_skill


class KnowledgeSearchInput(BaseModel):
    keywords: list[str] = Field(default_factory=list, description="ngram 全文词，AND 语义")
    query_text: str | None = Field(default=None, description="语义查询文本")
    limit: int = Field(default=5, ge=1, le=50)


class KnowledgeHit(BaseModel):
    doc_id: int
    title: str | None = None
    sem_dist: Decimal | None = None


class KnowledgeSearchOutput(BaseModel):
    hits: list[KnowledgeHit] = Field(default_factory=list)
    plan: str = ""
    elapsed_ms: float = 0.0


@register_skill
class KnowledgeSearchSkill(SpatialSkill[KnowledgeSearchInput, KnowledgeSearchOutput]):
    """knowledge_doc：全文(ngram) + 向量(cosine ANN)，实测全文谓词进入 PRE-FILTER。"""

    metadata: SkillMetadata
    input_schema = KnowledgeSearchInput
    output_schema = KnowledgeSearchOutput

    def __init__(self) -> None:
        self.metadata = SkillMetadata(
            name="knowledge_search",
            version="1.0.0",
            scene="atomic",
            description="知识库混合检索：全文(ngram) + 向量(cosine ANN)，一条 SQL 下推",
            intent_examples=["咖啡馆选址要注意什么", "客流怎么评估", "竞品密度怎么算"],
        )

    def build_plan(self, ctx: SkillContext) -> list[dict]:
        return [{"id": self.metadata.name, "skill": self.metadata.name}]

    def run(self, inputs: KnowledgeSearchInput, ctx: SkillContext) -> KnowledgeSearchOutput:
        svc = ctx.services
        qvec = svc.embedding.embed_query(inputs.query_text) if inputs.query_text else None
        spec = HybridQuerySpec(
            spatial=SpatialConstraint(),
            scalar=ScalarConstraint(),
            semantic=SemanticConstraint(
                keywords=inputs.keywords, query_text=inputs.query_text, limit=inputs.limit),
            target="knowledge_doc",
        )
        cq, plan = svc.query_builder.build_and_check(spec, qvec)
        rows, ms = svc.ob.execute_timed(cq.sql, cq.params)
        hits = [KnowledgeHit(
            doc_id=int(r["doc_id"]), title=r.get("title"), sem_dist=r.get("sem_dist"),
        ) for r in rows]
        return KnowledgeSearchOutput(hits=hits, plan=plan, elapsed_ms=round(ms, 2))
