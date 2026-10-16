"""约束点改的白名单校验模型（架构 §9.2「约束 chips 可直接点改」/ §9.6 裁决全在后端）。

任务卡 6：只有这四项允许用户点改，其余键一律拒（端点转 422）。区间口径能从既有
schema 读的就读，避免两处漂移：

  · `iso_minutes`  ← `ReachabilityInput.minutes`（ge=1, le=120）
  · `travel_mode`  ← `IsoMode`（高德距离测量无骑行档，卡 1 实测）
  · `min_rating`   ← `ScalarConstraint.min_rating`（ge=0, le=5，库内 rating 值域）
  · `radius_m`     比 `MultiModalSearchInput` 的 `le=100_000` **更严**（50 km 已是城市
                   尺度，chip 上给到 100 km 只会产出一屏无意义候选）——这条是点改
                   自己的口径，故显式写出。

不开放的键与理由（答辩要说话）：`keywords` 自由词进 ngram 全文极易 0 命中；
`center` 属地图手势（`draw_polygon` / `range_drag` 已有）；类目改动等价于重新提问；
`time_bucket` 是客流时段口径而非范围/门槛旋钮。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from backend.services.spatial_router import IsoMode


class ConstraintOverrides(BaseModel):
    """`constraint_edit` 载荷里的 `changes`（架构 §9.3 feedback 契约）。

    四项全为可选：只改一个键时其余键留 None，端点按 `exclude_none` 取子集下发。
    `extra="forbid"` 是白名单的**唯一**实现处——图里只与本节点已装配的 spec 取交集，
    不写第二份名单。
    """

    model_config = ConfigDict(extra="forbid")

    radius_m: int | None = Field(default=None, ge=100, le=50_000,
                                description="整数米：与意图槽位产出的 int 同型，chip 不该把半径改出小数")
    min_rating: float | None = Field(default=None, ge=0, le=5)
    iso_minutes: int | None = Field(default=None, ge=1, le=120)
    travel_mode: IsoMode | None = None


__all__ = ["ConstraintOverrides"]
