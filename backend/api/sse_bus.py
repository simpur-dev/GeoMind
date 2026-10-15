"""SSE 事件总线（架构 §9.3 事件类型表 + Last-Event-ID 断线续传）。

单进程实现：按 session 维护**有序事件缓冲**（单调 seq），SSE 端点以
100ms 粒度轮询增量。选轮询而非 asyncio.Queue 的理由：发布方可能在
FastAPI 的线程池（同步 chat 端点）里，Queue 的跨线程 put 需要
call_soon_threadsafe 包裹；轮询在演示规模（单用户/少会话）下零风险。

事件类型（架构 §9.3）：message.start / token / intent.updated /
constraints.updated / plan.updated / tool.start / tool.end / state.delta /
layer.update / map.view / decision.updated / interrupt.required /
message.end / error。

多 worker 部署时把 publish 换成 Redis Pub/Sub（保持接口不变即可）。
"""
from __future__ import annotations

import threading
import time
from typing import Any


class EventBus:
    """进程内 SSE 事件总线（线程安全）。"""

    def __init__(self, history_size: int = 500) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._size = history_size

    # ------------------------------------------------------------------
    def publish(self, session_id: str, type_: str, data: Any = None) -> int:
        """发布一条事件，返回其 seq（该 session 内从 1 单调递增）。"""
        with self._lock:
            buf = self._events.setdefault(session_id, [])
            seq = (buf[-1]["seq"] + 1) if buf else 1
            buf.append({"seq": seq, "type": type_, "data": data or {},
                        "ts": round(time.time(), 3)})
            if len(buf) > self._size:
                del buf[: len(buf) - self._size]
            return seq

    def since(self, session_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
        """取 seq > since_seq 的全部事件（断线续传重放）。"""
        with self._lock:
            return [e for e in self._events.get(session_id, []) if e["seq"] > since_seq]

    def last_seq(self, session_id: str) -> int:
        with self._lock:
            buf = self._events.get(session_id, [])
            return buf[-1]["seq"] if buf else 0

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._events.pop(session_id, None)


# 进程级单例：build_services 挂到 ServiceBundle.events，图节点与路由共用
_BUS = EventBus()


def get_bus() -> EventBus:
    return _BUS


def emit(ctx: Any, type_: str, data: Any = None) -> None:
    """图节点埋点助手：ctx 取不到 bus 时静默跳过（CLI/测试无总线）。"""
    bus = getattr(getattr(ctx, "services", None), "events", None)
    if bus is None:
        return
    sid = getattr(ctx, "session_id", "?")
    bus.publish(sid, type_, data)


__all__ = ["EventBus", "emit", "get_bus"]
