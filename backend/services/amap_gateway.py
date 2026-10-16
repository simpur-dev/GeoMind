"""高德适配网关（架构 §2.2 L4「高德适配网关」/ §7 对接边界）。

统一封装四件事：
  · key 与数字签名（2021-12-02 后申请的 Web 服务 key 建议配私钥签名）；
  · 缓存：按 §7 缓存策略分能力设 TTL（地理编码 7 天 / 路径按起终点+策略 /
    天气小时级 / 交通态势分钟级），进程内 TTL 字典，多 worker 时换 Redis；
  · 限流：进程内最小调用间隔（默认 QPS 3，个人 key 配额口径）；
  · 字段归一：高德各异构响应归一为统一 dict Schema（location=(lon, lat)）。

无 key（GEOMIND_AMAP_KEY 空）时 available=False，所有方法抛 AmapUnavailable——
调用方（空间计算路由 / 原子 Skill）据此走库内兜底，**不产生外部请求**。
网关失败抛 AmapApiError（含状态码与响应片段），不做静默兜底：降级是业务决定。
"""
from __future__ import annotations

import hashlib
import threading
import time
from typing import Any
from urllib.parse import quote

import httpx

from backend.config import Settings, get_settings

_AMAP_BASE = "https://restapi.amap.com"


class AmapUnavailable(Exception):
    """未配置高德 key（GEOMIND_AMAP_KEY），调用方应走库内兜底。"""


class AmapApiError(RuntimeError):
    """高德 API 调用失败（网络/HTTP 状态/业务 status!=1）。"""


def amap_signature(params: dict[str, str], private_key: str) -> str:
    """高德数字签名：参数按 key 升序 → k=v&... → URL 编码 → 追加私钥 → md5。"""
    qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in sorted(params.items()))
    return hashlib.md5((qs + private_key).encode("utf-8")).hexdigest()


class AmapGateway:
    """同步网关；transport 参数仅供测试注入（httpx.MockTransport）。"""

    # 各能力默认缓存 TTL（秒）——架构 §7 缓存策略列
    _TTL = {"geocode": 7 * 86400, "regeo": 7 * 86400, "place": 3600,
            "route": 3600, "distance": 600, "weather": 3600, "traffic": 60}

    def __init__(self, settings: Settings | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.s = settings or get_settings()
        self.available = bool(self.s.amap_key)
        self._transport = transport
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._min_interval = 1.0 / max(self.s.amap_qps, 0.1)
        self._last_call = 0.0

    # ------------------------------------------------------------------
    # 底层
    # ------------------------------------------------------------------
    def _request(self, path: str, params: dict[str, Any], cache_kind: str) -> dict[str, Any]:
        """带缓存 + 限流 + 签名的统一 GET。调用方负责 status==1 之外的归一。"""
        clean = {k: v for k, v in params.items() if v is not None}
        key = f"{path}:{sorted(clean.items())}"
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]

        if not self.available:
            raise AmapUnavailable("未配置 GEOMIND_AMAP_KEY，高德在线能力不可用")

        full = {**clean, "key": self.s.amap_key, "output": "json"}
        if self.s.amap_signature_secret:
            full["sig"] = amap_signature(
                {k: str(v) for k, v in full.items()}, self.s.amap_signature_secret)

        with self._lock:  # 进程内限流（演示规模：串行 + 最小间隔足够）
            wait = self._min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()

        try:
            # httpx 顶层 get() 不支持 transport 注入，须走 Client（测试用 MockTransport）
            with httpx.Client(timeout=10.0, transport=self._transport) as client:
                resp = client.get(f"{_AMAP_BASE}{path}", params=full)
        except httpx.HTTPError as exc:
            raise AmapApiError(f"高德网络错误: {exc}") from exc
        if resp.status_code != 200:
            raise AmapApiError(f"高德 HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        if str(data.get("status")) != "1":
            raise AmapApiError(f"高德业务错误 {data.get('infocode')}: {data.get('info')}")

        ttl = self._TTL.get(cache_kind, 600)
        with self._lock:
            self._cache[key] = (time.monotonic() + ttl, data)
        return data

    @staticmethod
    def _loc(text: str) -> tuple[float, float] | None:
        """高德 "lon,lat" 字符串 → 元组；非法返回 None。"""
        try:
            lon, lat = (float(x) for x in text.split(","))
            return (lon, lat)
        except (ValueError, AttributeError):
            return None

    # ------------------------------------------------------------------
    # 能力方法（§7 表格逐项；全部返回归一化 dict / None=无结果）
    # ------------------------------------------------------------------
    def geocode(self, address: str, city: str = "") -> tuple[float, float] | None:
        """地理编码：地址 → GCJ-02 坐标。缓存 7 天（§7）。"""
        d = self._request("/v3/geocode/geo", {"address": address, "city": city}, "geocode")
        geos = d.get("geocodes") or []
        return self._loc(geos[0]["location"]) if geos else None

    def regeocode(self, lon: float, lat: float) -> dict[str, str] | None:
        """逆地理编码：坐标 → 地址与 adcode。仅对最终展示候选调用（§7）。"""
        d = self._request("/v3/geocode/regeo", {"location": f"{lon},{lat}"}, "regeo")
        rg = (d.get("regeocode") or {})
        addr = rg.get("addressComponent") or {}
        if not addr:
            return None
        return {"address": str(rg.get("formatted_address", "")),
                "district": str(addr.get("district", "")),
                "adcode": str(addr.get("adcode", ""))}

    def place_around(self, location: tuple[float, float], radius_m: int = 1000,
                     types: str = "", keywords: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """周边搜索：GCJ-02 中心 + 半径 → POI 列表（库内没有的新鲜 POI 才用）。"""
        d = self._request("/v3/place/around", {
            "location": f"{location[0]},{location[1]}", "radius": min(radius_m, 50_000),
            "types": types, "keywords": keywords, "offset": min(limit, 25), "page": 1,
        }, "place")
        return [{"name": str(p.get("name", "")),
                 "location": self._loc(p.get("location", "")),
                 "address": str(p.get("address", "")),
                 "typecode": str(p.get("typecode", "")),
                 "tel": str(p.get("tel", ""))}
                for p in (d.get("pois") or [])][:limit]

    def route_plan(self, mode: str, origin: tuple[float, float],
                   destination: tuple[float, float]) -> dict[str, Any] | None:
        """路径规划（驾车/步行/骑行，§7 按起终点+策略缓存）。

        归一化：distance_m / duration_s / polyline（GCJ-02 折线，前端 route kind 直用）。
        """
        paths = {"driving": "/v3/direction/driving", "walking": "/v3/direction/walking",
                 "bicycling": "/v4/direction/bicycling"}
        if mode not in paths:
            raise ValueError(f"不支持的出行方式: {mode}")
        d = self._request(paths[mode], {
            "origin": f"{origin[0]},{origin[1]}",
            "destination": f"{destination[0]},{destination[1]}",
            **({"extensions": "base"} if mode != "bicycling" else {}),
        }, "route")
        if mode == "bicycling":  # v4 结构：data.paths
            p = ((d.get("data") or {}).get("paths") or [None])[0]
        else:  # v3 结构：route.paths
            p = ((d.get("route") or {}).get("paths") or [None])[0]
        if not p:
            return None
        return {"distance_m": float(p.get("distance", 0)),
                "duration_s": float(p.get("duration", 0)),
                "polyline": [(self._loc(seg) or (0.0, 0.0))
                             for step in (p.get("steps") or [])
                             for seg in str(step.get("polyline", "")).split(";")]
                if mode != "bicycling" else []}

    def distance_matrix(self, origins: list[tuple[float, float]],
                        destination: tuple[float, float], mode: int = 1) -> list[dict[str, Any]]:
        """距离测量（type: 1驾车 0步行 3骑行）→ 每个 origin 的距离/耗时。批量筛选用。"""
        if not 1 <= len(origins) <= 100:
            raise ValueError("origins 数量须在 1-100（高德单次上限）")
        d = self._request("/v3/distance", {
            "origins": "|".join(f"{o[0]},{o[1]}" for o in origins),
            "destination": f"{destination[0]},{destination[1]}", "type": mode,
        }, "distance")
        out: list[dict[str, Any]] = []
        for r in (d.get("results") or []):
            # 高德 origin_id 是起点序号（从 1 起）→ 归一化为 origins 下标
            try:
                idx = int(r.get("origin_id", 0)) - 1
            except (TypeError, ValueError):
                idx = -1
            out.append({"origin_index": idx if 0 <= idx < len(origins) else None,
                        "distance_m": float(r.get("distance", 0) or 0),
                        "duration_s": float(r.get("duration", 0) or 0)})
        return out

    def weather(self, adcode: str, extensions: str = "base") -> dict[str, Any] | None:
        """天气查询（小时级缓存 §7）。extensions: base 实况 / all 预报。"""
        d = self._request("/v3/weather/weatherInfo", {"city": adcode, "extensions": extensions},
                          "weather")
        lives = d.get("lives") or []
        return lives[0] if lives else (d.get("forecasts") or [None])[0]


__all__ = ["AmapApiError", "AmapGateway", "AmapUnavailable", "amap_signature"]
