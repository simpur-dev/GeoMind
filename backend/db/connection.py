"""L4 数据访问：SeekDB 连接层。

设计约束（架构 §8 DB 访问行 + 本机实测）：
  · pymysql + MySQL 协议，DictCursor 返回 list[dict]
  · 空密码 + utf8mb4；SeekDB 不启用 SSL，无需传 ssl 参数
  · 简单连接池（LifoQueue，线程安全），查询完归还；不裸连
  · 计时一律服务端 SYSDATE(6) —— NOW(6) 在 OB 返回语句开始时刻，
    短查询会算出负值（本机实测 -0.164 ms，见开发进度.md 踩坑记录）
  · 所有查询参数化，禁止字符串拼接用户输入
"""
from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from queue import LifoQueue
from typing import Any

import pymysql
from pymysql.cursors import DictCursor

from backend.config import Settings, get_settings

log = logging.getLogger(__name__)


class OBError(RuntimeError):
    """数据库访问错误基类。"""


class OBConnection:
    """带连接池的 SeekDB 访问入口。

    用法::

        ob = OBConnection()
        rows = ob.execute("SELECT poi_id FROM dim_poi WHERE poi_id = %(i)s", {"i": 1})
        rows, ms = ob.execute_timed("SELECT COUNT(*) AS c FROM dim_poi")
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self._pool: LifoQueue[pymysql.connections.Connection] = LifoQueue()
        self._lock = threading.Lock()
        self._created = 0

    # ---------- 连接管理 ----------
    def _new_conn(self) -> pymysql.connections.Connection:
        return pymysql.connect(
            host=self.s.ob_host,
            port=self.s.ob_port,
            user=self.s.ob_user,
            password=self.s.ob_password,
            database=self.s.ob_database,
            charset=self.s.ob_charset,
            cursorclass=DictCursor,
            connect_timeout=self.s.ob_connect_timeout_s,
            read_timeout=self.s.ob_read_timeout_s,
            autocommit=True,
        )

    @contextmanager
    def session(self) -> Iterator[pymysql.connections.Connection]:
        """借出连接，用完归还（异常时回滚并归还）。"""
        conn = self._pool.get()
        try:
            yield conn
        except Exception:
            with contextlib.suppress(Exception):  # 连接已断时 rollback 无意义
                conn.rollback()
            raise
        finally:
            self._pool.put(conn)

    def _acquire(self) -> pymysql.connections.Connection:
        with self._lock:
            if self._pool.empty() and self._created < self.s.ob_pool_size:
                self._created += 1
                need_new = True
            else:
                need_new = False
        if need_new:
            return self._new_conn()
        return self._pool.get()

    # ---------- 查询 ----------
    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """参数化查询，返回 list[dict]。"""
        conn = self._acquire()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params or {})
                return list(cur.fetchall())
        except pymysql.Error as exc:
            raise OBError(f"execute failed: {exc}") from exc
        finally:
            self._pool.put(conn)

    def execute_timed(self, sql: str, params: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], float]:
        """带服务端计时的查询：返回 (rows, elapsed_ms)。

        SYSDATE(6) 打点与目标查询在同一条连接上执行，避免跨连接时钟差异。
        """
        conn = self._acquire()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT SYSDATE(6) AS t0")
                t0 = cur.fetchone()["t0"]
                cur.execute(sql, params or {})
                result = list(cur.fetchall())
                cur.execute("SELECT SYSDATE(6) AS t1")
                t1 = cur.fetchone()["t1"]
            ms = (t1 - t0).total_seconds() * 1000.0
            return result, ms
        except pymysql.Error as exc:
            raise OBError(f"execute_timed failed: {exc}") from exc
        finally:
            self._pool.put(conn)

    def explain(self, sql: str, params: dict[str, Any] | None = None) -> str:
        """返回 EXPLAIN 计划全文。

        OB 的 EXPLAIN 返回**多行单列**（列名 'Query Plan'，每行是计划的一部分），
        必须按行拼接——只取第一行会拿到 '====' 分隔线（本项目实际踩过）。
        """
        rows = self.execute("EXPLAIN " + sql, params)
        return "\n".join(str(next(iter(r.values()))) for r in rows if r)

    def ping(self) -> bool:
        """健康检查。"""
        try:
            self.execute("SELECT 1 AS ok")
            return True
        except OBError:
            return False

    def close(self) -> None:
        while not self._pool.empty():
            conn = self._pool.get()
            with contextlib.suppress(Exception):
                conn.close()
        self._created = 0
