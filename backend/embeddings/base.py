"""Embedding Provider 抽象。

架构 §2.2 L6：Embedding 服务（本地 BGE-m3 或 API），1024 维。
阶段 0 无真实模型，用 SyntheticEmbeddingProvider —— 它**直接复用**
backend/etl/seed_cd.py 的段池构造函数（同一个 seed、同一个随机流），
因此查询向量与库内 2 万合成向量严格同分布，检索结果可复现、可解释。

诚实边界（与 seed_cd.py 头部声明一致）：
  SyntheticProvider 只用于验证索引、算子与链路，**不是真实语义模型**。
  对外表述必须注明"合成向量"；真实场景切换 GEOMIND_EMBEDDING_PROVIDER=bge，
  表结构与 SQL 零改动。
"""
from __future__ import annotations

import random
from abc import ABC, abstractmethod

from backend.config import Settings, get_settings
from backend.etl.seed_cd import ALL_TAGS, CATEGORIES, KB_TOPICS, build_segments, gen_query_vector, gen_vector

# 语义词表：查询文本 → 合成向量因子。与 seed_cd.py 的因子命名严格对应。
CATEGORY_WORDS: dict[str, str] = {
    "咖啡": "cat1", "咖啡馆": "cat1", "咖啡茶饮": "cat1",
    "火锅": "cat2", "串串": "cat2", "麻辣": "cat2",
    "茶饮": "cat3", "奶茶": "cat3", "果茶": "cat3",
    "便利店": "cat4", "超市": "cat4",
    "自习": "cat5", "自习室": "cat5", "培训": "cat5",
    "影城": "cat6", "电影": "cat6", "影院": "cat6",
    "健身": "cat7", "健身房": "cat7", "运动": "cat7",
    "美发": "cat8", "理发": "cat8", "造型": "cat8",
    "口腔": "cat9", "牙科": "cat9", "看牙": "cat9",
    "酒店": "cat10", "住宿": "cat10", "旅馆": "cat10",
}
TAG_WORDS: dict[str, str] = {
    "安静": "tag_quiet", "静谧": "tag_quiet", "静音": "tag_quiet", "清净": "tag_quiet",
    "自习": "tag_study", "学习": "tag_study", "考研": "tag_study", "看书": "tag_study",
    "停车": "tag_parking", "车位": "tag_parking",
    "24小时": "tag_h24", "24h": "tag_h24", "全天": "tag_h24", "通宵": "tag_h24",
    "宠物": "tag_pet", "猫": "tag_pet", "狗": "tag_pet",
    "网红": "tag_net", "打卡": "tag_net", "出片": "tag_net",
    "亲子": "tag_family", "儿童": "tag_family", "带娃": "tag_family",
    "商务": "tag_biz", "办公": "tag_biz", "会谈": "tag_biz",
    "平价": "tag_cheap", "便宜": "tag_cheap", "实惠": "tag_cheap",
}


class EmbeddingProvider(ABC):
    """文本 → 1024 维向量字面量（'[0.1,0.2,...]'）。"""

    name: str = "abstract"
    dim: int = 1024

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()

    @abstractmethod
    def embed_query(self, text: str) -> str:
        """查询向量：确定性、无噪声，用于 ORDER BY cosine_distance APPROXIMATE。"""

    @abstractmethod
    def embed_doc(self, text: str) -> str:
        """文档向量：用于写入（knowledge_doc 回写等），分布与库内文档一致。"""


class SyntheticEmbeddingProvider(EmbeddingProvider):
    """确定性合成向量（与 seed_cd.py 同源，见模块 docstring）。"""

    name = "synthetic"

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__(settings)
        # 与 seed_cd.main() 完全相同的 seed 与随机流 → proto 段逐字节一致
        rnd = random.Random(20261015)
        self._proto, self._noise = build_segments(rnd)

    # ------------------------------------------------------------------
    def _factors(self, text: str) -> tuple[str | None, list[str]]:
        low = text.lower()
        cat_factor: str | None = None
        tag_factors: list[str] = []
        for word, factor in CATEGORY_WORDS.items():
            if word in low or word in text:
                cat_factor = cat_factor or factor
                break
        for word, factor in TAG_WORDS.items():
            if (word in low or word in text) and factor not in tag_factors:
                tag_factors.append(factor)
        return cat_factor, tag_factors

    def embed_query(self, text: str) -> str:
        cat_factor, tag_factors = self._factors(text)
        if cat_factor is None and not tag_factors:
            # 无语义命中：退化为纯噪声查询向量（仍可跑通链路，结果无业务意义）
            rnd = random.Random(hash(text) & 0xFFFF)
            parts = []
            for s in range(32):
                parts.append(self._noise[s][rnd.randrange(500)])
            return "[" + ",".join(parts) + "]"
        cf = cat_factor or ("cat1" if tag_factors else "store")
        return gen_query_vector(self._proto, cf, tag_factors)

    def embed_doc(self, text: str) -> str:
        cat_factor, tag_factors = self._factors(text)
        cf = cat_factor or "kb"
        return gen_vector(random.Random(hash(text) & 0xFFFF), self._proto, self._noise, cf, tag_factors)


class BGEProvider(EmbeddingProvider):
    """真实 BGE-m3 占位实现。

    接入方式：本地 sentence-transformers 或 API，dim=1024（架构 §8）。
    当前未实现——阶段 0 无模型依赖，保留接口以便无缝切换。
    """

    name = "bge"

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()

    def embed_query(self, text: str) -> str:  # pragma: no cover - 未接入
        raise NotImplementedError("BGEProvider 尚未接入：pip install sentence-transformers 后实现")

    def embed_doc(self, text: str) -> str:  # pragma: no cover - 未接入
        raise NotImplementedError("BGEProvider 尚未接入")


_PROVIDERS: dict[str, type[EmbeddingProvider]] = {
    "synthetic": SyntheticEmbeddingProvider,
    "bge": BGEProvider,
}


def get_provider(settings: Settings | None = None) -> EmbeddingProvider:
    s = settings or get_settings()
    cls = _PROVIDERS.get(s.embedding_provider)
    if cls is None:
        raise ValueError(f"未知 embedding_provider: {s.embedding_provider}（可选 {list(_PROVIDERS)}）")
    return cls(s)


__all__ = [
    "ALL_TAGS", "CATEGORIES", "KB_TOPICS",
    "EmbeddingProvider", "SyntheticEmbeddingProvider", "BGEProvider", "get_provider",
]
