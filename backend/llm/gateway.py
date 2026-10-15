"""LLM 网关（架构 §2.2 L2 LLM 网关 / §8 LLM 网关行）。

阶段 0 不接真实模型（无 key），用 FakeLLMGateway 跑通全链路；
真实 DoubaoGateway / DeepSeekGateway 按 OpenAI 兼容协议留骨架。
空间相关调用约定 temperature≈0（架构 §2.2）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from backend.config import Settings, get_settings


class LLMMessage(BaseModel):
    role: str
    content: str


class LLMGateway(ABC):
    """模型可插拔网关。结构化输出用 Pydantic/JSON Schema 约束（架构 §8）。"""

    name: str = "abstract"

    @abstractmethod
    def chat(self, messages: list[LLMMessage], *, temperature: float = 0.0,
             response_schema: dict[str, Any] | None = None) -> str:
        """返回文本；response_schema 给定时应输出符合 schema 的 JSON 字符串。"""


class FakeLLMGateway(LLMGateway):
    """模板化假网关：把决策特征拼成可解释结论。

    不调用任何外部服务；仅用于阶段 0 跑通「决策综合」节点与测试。
    """

    name = "fake"

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()

    def chat(self, messages: list[LLMMessage], *, temperature: float = 0.0,
             response_schema: dict[str, Any] | None = None) -> str:
        last = messages[-1].content if messages else ""
        # 从最后一条消息里提取特征行（约定：以 "FEATURES:" 开头的内容原样透传并加结论）
        if "FEATURES:" in last:
            feats = last.split("FEATURES:", 1)[1].strip()
            return (f"结论：基于以下决策特征给出可解释建议 —— {feats}。"
                    "（本条由 FakeLLMGateway 生成，接入真实模型后由 LLM 综合。）")
        return f"（FakeLLMGateway 未识别到特征输入：{last[:80]}）"


class OpenAICompatGateway(LLMGateway):
    """豆包 / DeepSeek 等 OpenAI 兼容网关骨架（阶段 0 不实例化）。"""

    name = "openai_compat"

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        if not self.s.llm_api_key:
            raise ValueError("未配置 GEOMIND_LLM_API_KEY，无法实例化真实 LLM 网关")

    def chat(self, messages: list[LLMMessage], *, temperature: float = 0.0,
             response_schema: dict[str, Any] | None = None) -> str:
        raise NotImplementedError("真实 LLM 网关待接入（阶段 0 无 key）")


def get_gateway(settings: Settings | None = None) -> LLMGateway:
    s = settings or get_settings()
    if s.llm_provider == "fake":
        return FakeLLMGateway(s)
    return OpenAICompatGateway(s)
