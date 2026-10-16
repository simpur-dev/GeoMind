"""LLM 意图解析器单测：注入 stub gateway，不产生任何真实外部请求。

覆盖：合法 JSON 抽取、垃圾回复/网关异常降级 rule 并打标、两个解析器新槽位
（iso_minutes / travel_mode）同句同果、骑行近似标注、gesture 合并、工厂切换。
"""
from __future__ import annotations

import pytest

from backend.brain.intent import (
    LLMIntentParser,
    RuleIntentParser,
    make_intent_parser,
)
from backend.config import Settings
from backend.llm.gateway import LLMGateway, LLMMessage


class StubGateway(LLMGateway):
    """罐头回复网关：chat 原样返回预置文本，并记录调用次数。"""

    name = "stub"

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def chat(self, messages: list[LLMMessage], *, temperature: float = 0.0,
             response_schema: dict | None = None) -> str:
        self.calls += 1
        return self.reply


class BoomGateway(LLMGateway):
    name = "boom"

    def chat(self, messages: list[LLMMessage], *, temperature: float = 0.0,
             response_schema: dict | None = None) -> str:
        raise RuntimeError("LLM 挂了")


def _llm_reply(place: str = "春熙路", mode: str = "步行", minutes: int = 10) -> str:
    return (
        f'{{"scene": "hybrid_search", "place_name": "{place}", "category_word": "咖啡",'
        f' "radius_m": null, "iso_minutes": {minutes}, "travel_mode": "{mode}",'
        f' "keywords": ["安静"]}}'
    )


def test_llm_valid_json_extracts_slots():
    """合法 JSON：词槽 → 代码侧解析（坐标来自词表，模型不产坐标）。"""
    gw = StubGateway(_llm_reply())
    r = LLMIntentParser(Settings(), gateway=gw).parse("春熙路步行10分钟内有什么咖啡馆")
    assert gw.calls == 1
    assert r.slots["center"] == [104.0817, 30.657]        # PLACE_COORDS 词表解析
    assert r.slots["center_name"] == "春熙路"
    assert r.slots["category_id"] == 1 and r.slots["category_word"] == "咖啡"
    assert r.slots["iso_minutes"] == 10
    assert r.slots["travel_mode"] == "walking"            # 原词「步行」经映射表
    assert not r.question                                 # 空间+目标齐备，不追问
    assert any(h.startswith("llm:") for h in r.hits)
    assert any(h.startswith("mode:步行→walking") for h in r.hits)


def test_llm_garbage_falls_back_to_rule():
    """回复无 JSON → 降级 RuleIntentParser 同句解析，hits 打 llm_fallback 标。"""
    gw = StubGateway("抱歉，我不太明白你的意思。")
    r = LLMIntentParser(Settings(), gateway=gw).parse("春熙路附近的咖啡馆")
    rule = RuleIntentParser(Settings()).parse("春熙路附近的咖啡馆")
    assert any(h.startswith("llm_fallback:") for h in r.hits)
    assert r.slots == rule.slots and r.scene == rule.scene


def test_llm_gateway_error_falls_back():
    gw = BoomGateway()
    r = LLMIntentParser(Settings(), gateway=gw).parse("春熙路附近的咖啡馆")
    assert any(h.startswith("llm_fallback:RuntimeError") for h in r.hits)
    assert r.slots.get("center") == [104.0817, 30.657]    # rule 兜底照常工作


@pytest.mark.parametrize("query,minutes,mode,word", [
    ("春熙路步行15分钟内的咖啡馆", 15, "walking", "步行"),
    ("春熙路开车10分钟的咖啡馆", 10, "driving", "开车"),
])
def test_time_slots_parity_both_parsers(query, minutes, mode, word):
    """新槽位两个解析器同句同果（卡 4 验收硬指标）。"""
    rule = RuleIntentParser(Settings()).parse(query)
    llm = LLMIntentParser(
        Settings(), gateway=StubGateway(_llm_reply(mode=word, minutes=minutes))).parse(query)
    for r in (rule, llm):
        assert r.slots["iso_minutes"] == minutes
        assert r.slots["travel_mode"] == mode


def test_rule_bicycle_maps_to_driving_with_note():
    """骑行 → driving 宁大勿小（distance API 无骑行档），hits 必须标近似。"""
    r = RuleIntentParser(Settings()).parse("春熙路骑行15分钟的火锅")
    assert r.slots["travel_mode"] == "driving"
    assert any("骑行→driving" in h and "近似" in h for h in r.hits)


def test_llm_place_unresolved_still_asks_region():
    """LLM 听到词表外的地名：不产坐标，充分性追问区域（不猜测）。"""
    gw = StubGateway('{"scene": "hybrid_search", "place_name": "熊猫基地",'
                     ' "category_word": "咖啡", "iso_minutes": null,'
                     ' "travel_mode": null, "keywords": []}')
    r = LLMIntentParser(Settings(), gateway=gw).parse("熊猫基地附近的咖啡馆")
    assert r.question and "区域" in r.question
    assert "center" not in r.slots


def test_llm_gesture_overrides_text_place():
    """手势坐标覆盖文本地点（与规则版同一合并语义：坐标覆盖、名称保留首源）。"""
    gw = StubGateway(_llm_reply())
    r = LLMIntentParser(Settings(), gateway=gw).parse(
        "春熙路附近的咖啡馆", gesture={"center": [104.0665, 30.6575]})
    rule = RuleIntentParser(Settings()).parse(
        "春熙路附近的咖啡馆", gesture={"center": [104.0665, 30.6575]})
    assert r.slots["center"] == [104.0665, 30.6575]
    assert r.slots["center_name"] == rule.slots["center_name"]   # 合并语义两版一致


def test_factory_switch():
    """make_intent_parser 按配置切换（graph 唯一入口）。"""
    assert make_intent_parser(Settings(intent_backend="rule")).name == "rule"
    assert make_intent_parser(Settings(intent_backend="llm")).name == "llm"
