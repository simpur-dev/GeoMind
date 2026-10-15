"""LLM 网关（架构 §2.2 L2 LLM 网关 / §8 LLM 网关行）。

无 key（GEOMIND_LLM_PROVIDER=fake，默认）用 FakeLLMGateway 跑通全链路；
配置 key 后走 OpenAICompatGateway（DeepSeek / 豆包等 OpenAI 兼容协议）。
空间相关调用约定 temperature≈0（架构 §2.2）；网关失败抛 RuntimeError，
由调用方（decide 节点）降级为模板结论，保证 7 步图离线仍可跑通。
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
    """豆包 / DeepSeek 等 OpenAI 兼容网关（POST {base_url}/chat/completions）。

    base_url / model 不配时按 provider 给默认值：
      · deepseek → https://api.deepseek.com + deepseek-chat
      · doubao  → https://ark.cn-beijing.volces.com/api/v3（model 填方舟接入点 id）
    失败抛 RuntimeError（含状态码/响应片段），由调用节点决定降级策略；
    网关本身不做静默兜底——降级是业务决定，不是传输层决定。
    """

    name = "openai_compat"

    _DEFAULTS = {
        "deepseek": ("https://api.deepseek.com", "deepseek-chat"),
        "doubao": ("https://ark.cn-beijing.volces.com/api/v3", ""),
    }

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        if not self.s.llm_api_key:
            raise ValueError("未配置 GEOMIND_LLM_API_KEY，无法实例化真实 LLM 网关")
        dft_url, dft_model = self._DEFAULTS.get(self.s.llm_provider, ("", ""))
        self.base_url = (self.s.llm_base_url or dft_url).rstrip("/")
        self.model = self.s.llm_model or dft_model
        if not self.base_url or not self.model:
            raise ValueError("llm_base_url / llm_model 未配置且该 provider 无默认值")

    def chat(self, messages: list[LLMMessage], *, temperature: float = 0.0,
             response_schema: dict[str, Any] | None = None) -> str:
        import json as _json

        import httpx

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.model_dump() for m in messages],
            "temperature": temperature,
        }
        if response_schema:
            # OpenAI 兼容面没有统一 json_schema 参数，用提示词约束近似（阶段 1 按需升级）
            payload["messages"] = payload["messages"] + [
                {"role": "system",
                 "content": "只输出符合以下 JSON Schema 的 JSON，不要输出其他文字："
                            + _json.dumps(response_schema, ensure_ascii=False)},
            ]

        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.s.llm_api_key}"},
                json=payload, timeout=httpx.Timeout(30.0, connect=5.0),
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"LLM 网关网络错误: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(f"LLM 网关 HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return str(resp.json()["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, ValueError) as exc:
            raise RuntimeError(f"LLM 网关响应结构异常: {resp.text[:200]}") from exc


def get_gateway(settings: Settings | None = None) -> LLMGateway:
    s = settings or get_settings()
    if s.llm_provider == "fake":
        return FakeLLMGateway(s)
    return OpenAICompatGateway(s)
