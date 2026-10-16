"""L2 意图理解器（架构 §2.2：自然语言 + 手势 → 场景识别 + 槽位填充）。

两个可互换解析器，同一 IntentResult 契约（§10「规则意图器…LLM 版可无缝替换」）：
  · RuleIntentParser：词表 + 正则，可解释可测试，零外部依赖（默认）；
  · LLMIntentParser：LLM 抽取**词槽**（地名/业态/时间/方式），坐标与类目 id 仍由
    代码侧词表解析——坐标是确定性数据，模型编经纬度是幻觉源。
  · LLM 任何失败（网络/非法 JSON）静默降级到规则版并在 hits 记 `llm_fallback`：
    意图失败不该打断 7 步图（与决策节点降级同哲学）。

歧义/信息不足时不猜测：confidence < 阈值即产出 question，由图节点 interrupt 追问
（对标 MCP Elicitation，架构 §2.2）。

时间约束槽位（§2.2 约束建模器的时间约束，与客流 time_bucket 严格分字段）：
  iso_minutes  等时圈时间预算（分钟）
  travel_mode  walking | driving（distance API 无骑行档，骑行→driving 宁大勿小）
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from backend.config import Settings, get_settings
from backend.llm.gateway import LLMGateway, LLMMessage, get_gateway

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
_WKT_RING_RE = re.compile(r"\(\((.*)\)\)", re.S)
_RING_PAIR_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)")

# ---- 时间约束槽位（卡 4）----
_MINUTES_RE = re.compile(r"(\d+)\s*分钟")
# 中文时间词（规则版兜底；LLM 版在提示词里教过同一换算表）
_MINUTES_WORDS: dict[str, int] = {"一刻钟": 15, "半个小时": 30, "半小时": 30,
                                  "一个小时": 60, "一小时": 60}
# 出行方式 → travel_mode。distance API 无骑行档：骑行映射 driving（宁大勿小，
# 步行会 3 倍低估），纯骑行点到点路径归 path_planner（v4 有骑行）。
_TRAVEL_WORDS: dict[str, str] = {
    "步行": "walking", "走路": "walking", "散步": "walking",
    "开车": "driving", "驾车": "driving", "自驾": "driving", "打车": "driving",
    "骑行": "driving", "骑车": "driving", "电动车": "driving",
}
_TRAVEL_APPROX = ("骑行", "骑车", "电动车")  # 这些词到 driving 是近似，须在 hits 标注


def _flat_dist_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """平面近似距离（米）。演示城市尺度（<0.5°）误差可忽略。"""
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians(lat1))
    dy = (lat2 - lat1) * 110_540
    return math.hypot(dx, dy)


def polygon_center_radius(wkt: str) -> tuple[tuple[float, float], float] | None:
    """POLYGON WKT → (去闭合顶点均值质心, 外接半径米)。

    L1 手势翻译的一部分（架构 §2.2）：框选是显式空间约束，但网格聚合层
    （spatial_join_agg）只走 grid_ids_in_radius 圆路径（压测验证过的写法），
    因此把手势多边形翻译为「质心 + 外接半径」。候选层仍用 ST_Contains
    多边形精确约束；外接半径保证圆 ⊇ 多边形，两段式 WHERE 下圆约束
    不会收紧多边形语义。
    """
    m = _WKT_RING_RE.search(wkt)
    if not m:
        return None
    pts = [(float(x), float(y))
           for x, y in _RING_PAIR_RE.findall(m.group(1))]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]  # 去掉闭合重复顶点，避免质心被加权偏移
    if len(pts) < 3:
        return None
    lon = sum(p[0] for p in pts) / len(pts)
    lat = sum(p[1] for p in pts) / len(pts)
    radius = max(_flat_dist_m(lon, lat, x, y) for x, y in pts)
    return (lon, lat), max(radius, 100.0)


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


class IntentParser(Protocol):
    """解析器协议：RuleIntentParser / LLMIntentParser 同契约可互换（§10）。"""

    name: str

    def parse(self, query: str, gesture: dict[str, Any] | None = None) -> IntentResult: ...


# ---------------------------------------------------------------------------
# 共享后处理（两个解析器必须走同一条路，防止行为漂移）
# ---------------------------------------------------------------------------
def _merge_gesture(slots: dict[str, Any], center: tuple[float, float] | None,
                   gesture: dict[str, Any] | None
                   ) -> tuple[tuple[float, float] | None, float, bool]:
    """手势约束合并（架构 §2.2 L1 手势约束翻译器的结构化产物）。

    框选/拖拽是用户显式动作：polygon_wkt / gesture center 本身就是空间约束，
    权重与文本地点词同级（0.2-0.3），且不再触发「要查哪个区域」追问。
    返回 (合并后 center, score 增量, gesture_spatial)。
    """
    score = 0.0
    gesture_spatial = False
    if not gesture:
        return center, score, gesture_spatial
    if gesture.get("center"):
        center = tuple(gesture["center"])
        slots["center_name"] = slots.get("center_name", "地图手势")
        score += 0.2
        gesture_spatial = True
    if gesture.get("radius_m"):
        slots["radius_m"] = int(gesture["radius_m"])
        score += 0.1
    if gesture.get("polygon_wkt"):
        slots["polygon_wkt"] = gesture["polygon_wkt"]
        score += 0.3
        gesture_spatial = True
        # 框选即空间约束：翻译出质心+外接半径供网格聚合层使用，并覆盖文本地点
        # center（手势是显式用户动作，优先级最高）
        derived = polygon_center_radius(gesture["polygon_wkt"])
        if derived is not None:
            center = derived[0]
            slots["center_name"] = "框选区域"
            slots["radius_m"] = int(derived[1])
    return center, score, gesture_spatial


def _finalize_slots(slots: dict[str, Any], center: tuple[float, float] | None,
                    text: str) -> None:
    """默认值注入（原地修改）：city / center 列表 / 默认半径 / 全文关键词。"""
    if "category_word" in slots:
        slots["keywords"] = [slots["category_word"]]
    slots["query_text"] = text
    slots["city"] = "成都"
    if center is not None:
        slots["center"] = list(center)
    if "radius_m" not in slots:
        slots["radius_m"] = 1500  # 默认 1.5 km（与演示口径一致）


def _apply_sufficiency(result: IntentResult, center: tuple[float, float] | None,
                       gesture_spatial: bool, settings: Settings) -> None:
    """充分性追问（原地修改 result）：不猜测，信息不足就 interrupt（§2.2）。

    判据 = 空间约束（center 或 polygon）∩ 检索目标（类目词 / 语义标签词 /
    选址场景词）。置信度阈值只对纯文本信号兜底；手势已显式给空间约束时不追问。
    """
    has_spatial = center is not None or bool(result.slots.get("polygon_wkt"))
    has_target = bool(result.slots.get("category_word") or result.slots.get("keywords")
                      or result.scene == "site_selection")
    if not has_spatial:
        result.confidence = min(result.confidence, 0.5)
        result.question = "要查哪个区域？可以说「春熙路附近」「建设路 1 公里」或直接在地图上框选。"
    elif not has_target:
        result.confidence = min(result.confidence, 0.4)
        result.question = "想找哪类点位？例如「咖啡馆」「火锅」「便利店」。"
    elif result.confidence < settings.intent_confidence_threshold and not gesture_spatial:
        result.question = "想找哪类点位？例如「咖啡馆」「火锅」「便利店」。"


def _extract_time_mode(text: str, slots: dict[str, Any], hits: list[str]) -> float:
    """时间预算与出行方式槽位（两个解析器共用同一换算/映射表）。返回 score 增量。"""
    score = 0.0
    m = _MINUTES_RE.search(text)
    if m:
        slots["iso_minutes"] = int(m.group(1))
        hits.append(f"minutes:{m.group(1)}")
        score += 0.1
    else:
        for word, minutes in _MINUTES_WORDS.items():
            if word in text:
                slots["iso_minutes"] = minutes
                hits.append(f"minutes:{word}={minutes}")
                score += 0.1
                break
    for word, mode in _TRAVEL_WORDS.items():
        if word in text:
            slots["travel_mode"] = mode
            hits.append(f"mode:{word}→{mode}" + ("（近似）" if word in _TRAVEL_APPROX else ""))
            score += 0.1
            break
    return score


class RuleIntentParser:
    """规则意图器：词表 + 正则，输出可解释。"""

    name = "rule"

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

        # 5) 时间预算与出行方式（卡 4 新增槽位，与 LLM 版同一换算表）
        score += _extract_time_mode(text, slots, hits)

        # 6) 语义标签词 → 只进 query_text（语义向量），**不进全文关键词**
        #    原因（实测踩坑）："安静/停车/wifi"等是 attrs 里的属性标签，不在 POI 名称里；
        #    若当成 ngram 全文词，'+安静 +自习' 的 AND 语义会命中 0 行。
        keywords = [w for w in TAG_WORDS if w in text]
        if keywords:
            hits.append("tags:" + ",".join(keywords))
            score += 0.1

        # 7) 手势合并（共享纯函数）
        center, g_score, gesture_spatial = _merge_gesture(slots, center, gesture)
        score += g_score

        # 8) 默认值注入 + 充分性追问（共享纯函数）
        _finalize_slots(slots, center, text)
        result = IntentResult(scene=scene, slots=slots,
                              confidence=round(min(score, 1.0), 2), hits=hits)
        _apply_sufficiency(result, center, gesture_spatial, self.s)
        return result


# ---------------------------------------------------------------------------
# LLM 意图解析器
# ---------------------------------------------------------------------------
_LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scene": {"type": "string", "enum": ["site_selection", "hybrid_search"]},
        "place_name": {"type": ["string", "null"],
                       "description": "提到的地名原词，如 春熙路/天府三街"},
        "category_word": {"type": ["string", "null"],
                          "description": "业态词，如 咖啡/火锅/便利店"},
        "radius_m": {"type": ["integer", "null"],
                     "description": "提到的半径数字（换算成米）"},
        "iso_minutes": {"type": ["integer", "null"],
                        "description": "时间预算分钟数；一刻钟=15，半小时=30"},
        "travel_mode": {"type": ["string", "null"],
                        "description": "出行方式原词：步行/走路/开车/驾车/骑行等"},
        "keywords": {"type": "array", "items": {"type": "string"},
                     "description": "属性词，如 安静/停车/自习"},
    },
    "required": ["scene"],
}

_LLM_PROMPT = (
    "你是空间检索意图解析器。从用户查询抽取字段，只输出一个 JSON 对象，不要输出其他文字：\n"
    '- scene: "site_selection"（选址/开店/位置类）或 "hybrid_search"（搜地方/找东西）\n'
    "- place_name: 提到的地名原词（没有则 null）\n"
    "- category_word: 业态词（没有则 null）\n"
    "- radius_m: 半径数字换算成米（没有则 null）\n"
    "- iso_minutes: 时间预算分钟数，一刻钟=15、半小时=30（没有则 null）\n"
    "- travel_mode: 出行方式原词（没有则 null）\n"
    "- keywords: 属性词数组（安静/停车/自习等，没有则空数组）\n"
    "用户查询：{query}"
)


def _extract_json(text: str) -> dict[str, Any]:
    """从 LLM 回复里容错抽取首个 JSON 对象（模型偶尔带前后缀说明）。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"回复中无 JSON：{text[:60]}")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("JSON 不是对象")
    return data


def _resolve_place(name: str | None) -> tuple[str, tuple[float, float]] | None:
    """地名 → PLACE_COORDS 词表解析（双向包含匹配）。解析不了返回 None。"""
    if not name:
        return None
    for place, xy in PLACE_COORDS.items():
        if place in name or name in place:
            return place, xy
    return None


class LLMIntentParser:
    """LLM 意图解析器（§2.2：语义歧义消解；§10：与规则版同契约无缝替换）。

    只抽取**词槽**，坐标/类目 id 由代码侧词表解析（铁律：模型不产数值标识）。
    gateway 参数供测试注入 stub；任何失败降级 RuleIntentParser 并记
    `llm_fallback:<原因>`——意图失败不打断 7 步图。
    """

    name = "llm"

    def __init__(self, settings: Settings | None = None,
                 gateway: LLMGateway | None = None) -> None:
        self.s = settings or get_settings()
        self._gw = gateway

    def _gateway(self) -> LLMGateway:
        return self._gw if self._gw is not None else get_gateway()

    def parse(self, query: str, gesture: dict[str, Any] | None = None) -> IntentResult:
        text = query.strip()
        try:
            raw = self._gateway().chat(
                [LLMMessage(role="user", content=_LLM_PROMPT.format(query=text))],
                temperature=0.0, response_schema=_LLM_SCHEMA)
            data = _extract_json(raw)
        except Exception as exc:  # noqa: BLE001 - 降级是契约的一部分（tests 钉住）
            fallback = RuleIntentParser(self.s).parse(query, gesture)
            fallback.hits.append(f"llm_fallback:{type(exc).__name__}: {exc}"[:100])
            return fallback
        return self._build_result(text, data, gesture)

    def _build_result(self, text: str, data: dict[str, Any], gesture: dict[str, Any] | None
                      ) -> IntentResult:
        hits: list[str] = ["llm:parsed"]
        slots: dict[str, Any] = {}
        score = 0.0

        scene = data.get("scene")
        scene = scene if scene in ("site_selection", "hybrid_search") else "hybrid_search"
        if scene == "site_selection":
            hits.append("scene:llm")
            score += 0.3

        # 地点：LLM 出词，代码出坐标（铁律：模型不产数值标识）
        center = None
        resolved = _resolve_place(data.get("place_name"))
        if resolved:
            pname, xy = resolved
            center = xy
            slots["center_name"] = pname
            hits.append(f"place:{pname}")
            score += 0.25
        elif data.get("place_name"):
            hits.append(f"place_unresolved:{data['place_name']}")
        if center is None:
            # LLM 地名没解析出来：确定性词表补扫原文（双保险）
            for place, xy in PLACE_COORDS.items():
                if place in text:
                    center = xy
                    slots["center_name"] = place
                    hits.append(f"place:{place}(词表补扫)")
                    score += 0.25
                    break

        cat = data.get("category_word")
        if cat and cat in CATEGORY_IDS:
            slots["category_id"] = CATEGORY_IDS[cat]
            slots["category_word"] = cat
            hits.append(f"cat:{cat}")
            score += 0.25
        elif cat:
            hits.append(f"cat_unresolved:{cat}")
        if "category_word" not in slots:
            # LLM 没抽出可解析业态：确定性词表补扫原文（双保险，LLM 为主词表为辅）
            for cword, cid in CATEGORY_IDS.items():
                if cword in text:
                    slots["category_id"] = cid
                    slots["category_word"] = cword
                    hits.append(f"cat:{cword}(词表补扫)")
                    score += 0.25
                    break

        if data.get("radius_m"):
            try:
                slots["radius_m"] = int(data["radius_m"])
                hits.append(f"radius:{slots['radius_m']}m")
                score += 0.15
            except (TypeError, ValueError):
                pass

        # 时间/方式：LLM 抽取值显式优先；缺失时与规则版同表扫原文。
        # 拼串重扫会在「LLM 听到步行、原文说开车」这类矛盾时胜出方随机（实测踩坑）。
        word: str | None = data.get("travel_mode")
        if word:
            mapped = _TRAVEL_WORDS.get(word)
            if mapped is None:
                mapped = next((v for k, v in _TRAVEL_WORDS.items() if k in word), None)
            if mapped:
                slots["travel_mode"] = mapped
                approx = any(a in word for a in _TRAVEL_APPROX)
                hits.append(f"mode:{word}→{mapped}" + ("（近似）" if approx else ""))
                score += 0.1
        minutes = data.get("iso_minutes")
        if minutes:
            try:
                slots["iso_minutes"] = int(minutes)
                hits.append(f"minutes:{slots['iso_minutes']}")
                score += 0.1
            except (TypeError, ValueError):
                pass
        elif not word:
            # 连方式都没听到才扫原文：顺便兜底「一刻钟」这类中文时间词
            score += _extract_time_mode(text, slots, hits)

        tags = [w for w in TAG_WORDS if w in text] + [
            k for k in (data.get("keywords") or []) if k and k not in TAG_WORDS]
        if tags:
            hits.append("tags:" + ",".join(tags))
            score += 0.1

        center, g_score, gesture_spatial = _merge_gesture(slots, center, gesture)
        score += g_score
        _finalize_slots(slots, center, text)

        result = IntentResult(scene=scene, slots=slots,
                              confidence=round(min(score, 1.0), 2), hits=hits)
        _apply_sufficiency(result, center, gesture_spatial, self.s)
        return result


def make_intent_parser(settings: Settings | None = None) -> IntentParser:
    """按配置选解析器（图节点唯一入口；节点不感知具体类型）。"""
    s = settings or get_settings()
    if s.intent_backend == "llm":
        return LLMIntentParser(s)
    return RuleIntentParser(s)
