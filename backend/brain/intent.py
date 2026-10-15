"""L2 意图理解器（架构 §2.2：自然语言 + 手势 → 场景识别 + 槽位填充）。

阶段 0 用规则实现（词表 + 正则），可解释、可测试、无外部依赖；
接口与 LLM 版本一致（同一 IntentResult），后续可无缝替换为 LLM 网关实现。

歧义/信息不足时不猜测：confidence < 阈值即产出 question，由图节点 interrupt 追问
（对标 MCP Elicitation，架构 §2.2）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from backend.config import Settings, get_settings

# 地名 → 坐标（成都主城，WGS84）。生产上应由地理编码服务/POI 库提供。
PLACE_COORDS: dict[str, tuple[float, float]] = {
    "春熙路": (104.0817, 30.6570), "太古里": (104.0817, 30.6570), "ifs": (104.0817, 30.6570),
    "天府广场": (104.0665, 30.6575), "天府三街": (104.0632, 30.5720),
    "金融城": (104.0632, 30.5720), "高新": (104.0632, 30.5720), "大源": (104.0400, 30.5450),
    "建设路": (104.1155, 30.6688), "金沙": (104.0186, 30.6717), "玉林": (104.0623, 30.6398),
    "犀浦": (103.9400, 30.7500), "龙泉": (104.2700, 30.5600), "双流": (104.0600, 30.5800),
    "锦里": (104.0560, 30.6450), "宽窄巷子": (104.0555, 30.6690), "九眼桥": (104.0750, 30.6390),
    "望平街": (104.0900, 30.6500), "兰桂坊": (104.0730, 30.6400), "电子科大": (104.1000, 30.6700),
    "川大": (104.0860, 30.6300), "财大": (104.1150, 30.6900), "交大": (104.0550, 30.7000),
}

# 类目词 → category_id（与 dim_category 一致）
CATEGORY_IDS: dict[str, int] = {
    "咖啡": 1, "咖啡馆": 1, "咖啡茶饮": 1,
    "火锅": 2, "串串": 2,
    "茶饮": 3, "奶茶": 3,
    "便利店": 4, "超市": 4,
    "自习": 5, "自习室": 5, "培训": 5,
    "影城": 6, "电影": 6,
    "健身": 7,
    "美发": 8, "理发": 8, "造型": 8,
    "口腔": 9, "牙科": 9,
    "酒店": 10, "住宿": 10,
}

# 语义标签词（用于全文关键词与合成向量因子）
TAG_WORDS: list[str] = ["安静", "静谧", "静音", "清净", "自习", "学习", "考研",
                        "停车", "车位", "24小时", "全天", "通宵", "宠物",
                        "网红", "打卡", "亲子", "儿童", "商务", "办公",
                        "平价", "便宜", "实惠"]

_SCENE_SITE = ("选址", "开在", "开店", "铺位", "门面", "推荐位置", "适合开",
               "选个", "位置", "哪儿开", "哪里开", "合适", "选址点")
_RADIUS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(公里|千米|km|米|m)", re.I)


@dataclass
class IntentResult:
    """意图识别结果。question 非空表示需要追问（图节点据此 interrupt）。"""

    scene: str = "hybrid_search"
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    question: str | None = None
    hits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"scene": self.scene, "slots": self.slots,
                "confidence": self.confidence, "question": self.question, "hits": self.hits}


class RuleIntentParser:
    """规则意图器：词表 + 正则，输出可解释。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()

    def parse(self, query: str, gesture: dict[str, Any] | None = None) -> IntentResult:
        text = query.strip()
        low = text.lower()
        hits: list[str] = []
        slots: dict[str, Any] = {}
        score = 0.0

        # 1) 场景路由
        scene = "hybrid_search"
        for kw in _SCENE_SITE:
            if kw in text:
                scene = "site_selection"
                hits.append(f"scene:{kw}")
                score += 0.3
                break

        # 2) 类目
        for word, cid in CATEGORY_IDS.items():
            if word in text:
                slots["category_id"] = cid
                slots["category_word"] = word
                hits.append(f"cat:{word}")
                score += 0.25
                break

        # 3) 地点 → center
        center = None
        for place, (lon, lat) in PLACE_COORDS.items():
            if place in text or place.lower() in low:
                center = (lon, lat)
                slots["center_name"] = place
                hits.append(f"place:{place}")
                score += 0.25
                break

        # 4) 半径
        m = _RADIUS_RE.search(text)
        if m:
            val = float(m.group(1))
            unit = m.group(2).lower()
            radius_m = val * 1000 if unit in ("公里", "千米", "km") else val
            slots["radius_m"] = int(radius_m)
            hits.append(f"radius:{int(radius_m)}m")
            score += 0.15

        # 5) 语义标签词 → 只进 query_text（语义向量），**不进全文关键词**
        #    原因（实测踩坑）："安静/停车/wifi"等是 attrs 里的属性标签，不在 POI 名称里；
        #    若当成 ngram 全文词，'+安静 +自习' 的 AND 语义会命中 0 行。
        #    全文关键词只放类目词（名称里一定出现的词），属性走语义检索。
        keywords = [w for w in TAG_WORDS if w in text]
        if keywords:
            hits.append("tags:" + ",".join(keywords))
            score += 0.1

        # 6) 手势约束（架构 §2.2 L1 手势约束翻译器的结构化产物）
        if gesture:
            if gesture.get("center"):
                center = tuple(gesture["center"])
                slots["center_name"] = slots.get("center_name", "地图手势")
                score += 0.2
            if gesture.get("radius_m"):
                slots["radius_m"] = int(gesture["radius_m"])
                score += 0.1
            if gesture.get("polygon_wkt"):
                slots["polygon_wkt"] = gesture["polygon_wkt"]
                score += 0.2

        # 7) 全文关键词 = 类目词（名称里一定出现）；语义文本 = 原始 query（含属性词）
        if "category_word" in slots:
            slots["keywords"] = [slots["category_word"]]
        slots["query_text"] = text

        slots["city"] = "成都"
        if center is not None:
            slots["center"] = list(center)
        if "radius_m" not in slots:
            slots["radius_m"] = 1500  # 默认 1.5 km（与演示口径一致）

        result = IntentResult(scene=scene, slots=slots, confidence=round(min(score, 1.0), 2), hits=hits)

        # 8) 信息不足 → 追问（不猜测）
        if center is None:
            result.confidence = min(result.confidence, 0.5)
            result.question = "要查哪个区域？可以说「春熙路附近」「建设路 1 公里」或直接在地图上框选。"
        elif result.confidence < self.s.intent_confidence_threshold:
            result.question = "想找哪类点位？例如「咖啡馆」「火锅」「便利店」。"
        return result
