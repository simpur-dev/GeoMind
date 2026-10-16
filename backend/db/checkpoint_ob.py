"""L5 检查点同库持久化：`OBCheckpointer`（架构 §2.2 L5「LangGraph checkpointer 同库持久化」）。

为什么值得自己写一个：会话记忆 / 故障恢复 / HITL 三件事在架构里都靠检查点，而进程内
`MemorySaver` 一重启就全丢——`interrupt()` 追问到一半重启后端，用户补答就无处可续。
同库持久化把这张表变成与 `scene_solution` 一样的 OceanBase 资产（不引 Redis/SQLite，
「单库多模」是本项目的差异化卖点）。

接口口径以**已安装版本**为准（langgraph 1.2.15 / langgraph-checkpoint 4.3.0，
实测记录见《docs/设计方案-OB同库检查点持久化.md》§1），三条决定了实现形态：

  · 序列化用基类自带的 `JsonPlusSerializer`：`dumps_typed()` 返回 `(type, bytes)`，
    实测 type 是 `msgpack` 而非 JSON 文本 → 类型标签与二进制列都要落表，不自造格式；
  · `checkpoint_id` 是 uuid6，**字典序即创建序** → 「取最新」用 `MAX/MAX(checkpoint_id)`，
    `list` 用 `ORDER BY checkpoint_id DESC`，不依赖墙钟精度；
  · 整份 checkpoint（含 `channel_values`）存一个 LONGBLOB，不按通道拆版本：
    基类的 delta 历史默认实现只沿 `parent_config` 走链并看通道值是否填充，全量 blob
    天然满足其终止条件；代价是每个 checkpoint 带全量状态（体积实测登记在设计文档 §8）。

写路径失败一律抛出（`OBError` 透传）：checkpoint 丢失比一次请求失败更危险，
这里不做静默降级。
"""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    PendingWrite,
    get_checkpoint_metadata,
)

from backend.db.connection import OBConnection

_CKPT_COLS = ("thread_id, checkpoint_id, parent_checkpoint_id, type, checkpoint, "
              "metadata_type, metadata")
_WRITES_ORDER = "task_path, task_id, idx, channel"   # 与 writes_sort_key 同一元组序

# 类型别名而不是就地写 list[...]：本类实现了契约要求的 `list()` 方法，
# 类体内后置注解里的 `list` 会被解析成这个方法而不是内置类型（mypy 实测报 valid-type）。
PendingWrites = list[PendingWrite]


class CheckpointStorageError(RuntimeError):
    """检查点存储层的用法错误（不是数据库故障——那种情况 OBError 原样透传）。"""


class OBCheckpointer(BaseCheckpointSaver[str]):
    """同步检查点器：一行一个 checkpoint + 一行一个待回放写入，都在 SeekDB。

    经 `OBConnection` 连接池执行（架构 §2.2 L6 单一数据访问入口），不自建连接。
    """

    def __init__(self, ob: OBConnection) -> None:
        super().__init__()
        self.ob = ob

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """取指定 checkpoint；未给 checkpoint_id 时取该 thread 字典序最大（=最新）的一条。"""
        thread_id, _ = self._thread(config)
        ckpt_id = config["configurable"].get("checkpoint_id")
        if ckpt_id:
            sql = (f"SELECT {_CKPT_COLS} FROM graph_checkpoint "
                   "WHERE thread_id = %(thread_id)s AND checkpoint_id = %(checkpoint_id)s")
            params: dict[str, Any] = {"thread_id": thread_id, "checkpoint_id": ckpt_id}
        else:
            sql = (f"SELECT {_CKPT_COLS} FROM graph_checkpoint WHERE thread_id = %(thread_id)s "
                   "ORDER BY checkpoint_id DESC LIMIT 1")
            params = {"thread_id": thread_id}
        rows = self.ob.execute(sql, params)
        if not rows:
            return None
        return self._to_tuple(rows[0])

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """按 checkpoint_id 倒序回放历史（时间旅行/调试用）。

        `before` 与 `limit` 下推到 SQL；`filter` 是 metadata 的等值条件，而 metadata 存的是
        二进制 blob，库里筛要额外冗余列——这里在 Python 侧筛（本图历史条数是个位数，
        与官方内存实现同一取舍）。
        """
        where: list[str] = []
        params: dict[str, Any] = {}
        if config and config.get("configurable", {}).get("thread_id"):
            where.append("thread_id = %(thread_id)s")
            params["thread_id"] = config["configurable"]["thread_id"]
            base_id = config["configurable"].get("checkpoint_id")
            if base_id:
                where.append("checkpoint_id <= %(base_id)s")
                params["base_id"] = base_id
        if before and before.get("configurable", {}).get("checkpoint_id"):
            where.append("checkpoint_id < %(before_id)s")
            params["before_id"] = before["configurable"]["checkpoint_id"]
        sql = f"SELECT {_CKPT_COLS} FROM graph_checkpoint"
        if where:
            sql += "\nWHERE " + "\n  AND ".join(where)
        sql += "\nORDER BY checkpoint_id DESC"
        if limit is not None:
            sql += "\nLIMIT %(limit)s"
            params["limit"] = max(0, int(limit))

        def gen() -> Iterator[CheckpointTuple]:
            for row in self.ob.execute(sql, params):
                tuple_ = self._to_tuple(row)
                if filter and any(tuple_.metadata.get(k) != v for k, v in filter.items()):
                    continue
                yield tuple_

        return gen()

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """整份 checkpoint 落一行；返回带新 checkpoint_id 的 config（pregel 要拿它当后续父指针）。

        `new_versions` 在本实现里不需要：通道值随整份 blob 一起存，不按 (通道, 版本) 拆行。
        """
        thread_id, ns = self._thread(config)
        ckpt_type, ckpt_blob = self.serde.dumps_typed(checkpoint)
        meta_type, meta_blob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        self.ob.execute(
            "INSERT INTO graph_checkpoint (thread_id, checkpoint_id, parent_checkpoint_id, "
            "type, checkpoint, metadata_type, metadata)\n"
            "VALUES (%(thread_id)s, %(checkpoint_id)s, %(parent_id)s, %(type)s, %(checkpoint)s, "
            "%(metadata_type)s, %(metadata)s)\n"
            "ON DUPLICATE KEY UPDATE parent_checkpoint_id = VALUES(parent_checkpoint_id), "
            "type = VALUES(type), checkpoint = VALUES(checkpoint), "
            "metadata_type = VALUES(metadata_type), metadata = VALUES(metadata)",
            {
                "thread_id": thread_id,
                "checkpoint_id": checkpoint["id"],
                "parent_id": config["configurable"].get("checkpoint_id"),
                "type": ckpt_type,
                "checkpoint": ckpt_blob,
                "metadata_type": meta_type,
                "metadata": meta_blob,
            },
        )
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns,
                                 "checkpoint_id": checkpoint["id"]}}

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """存中间写入——interrupt 的待回放态就在这里，丢了 resume 必错。

        两条语义分开（与框架约定一致）：
          · 特殊通道（`__error__/__scheduled__/__interrupt__/__resume__`）用固定**负**索引，
            同一 (task_id, idx, channel) 再写必须覆盖 → `ON DUPLICATE KEY UPDATE`；
          · 普通通道索引为正，重放同一位序视为同一逻辑值，先到先得 → `INSERT IGNORE`。
        """
        thread_id, _ = self._thread(config)
        ckpt_id = config["configurable"].get("checkpoint_id")
        if not ckpt_id:
            raise CheckpointStorageError("put_writes 需要 config.checkpoint_id")
        for pos, (channel, value) in enumerate(writes):
            idx = WRITES_IDX_MAP.get(channel, pos)
            tag, blob = self.serde.dumps_typed(value)
            params = {
                "thread_id": thread_id, "checkpoint_id": ckpt_id, "task_id": task_id,
                "idx": idx, "channel": channel, "task_path": task_path,
                "type": tag, "value_blob": blob,
            }
            head = ("INSERT IGNORE INTO" if idx >= 0 else "INSERT INTO")
            tail = ("" if idx >= 0 else
                    "\nON DUPLICATE KEY UPDATE type = VALUES(type), "
                    "value_blob = VALUES(value_blob), task_path = VALUES(task_path)")
            self.ob.execute(
                f"{head} graph_checkpoint_writes (thread_id, checkpoint_id, task_id, idx, "
                f"channel, task_path, type, value_blob)\n"
                "VALUES (%(thread_id)s, %(checkpoint_id)s, %(task_id)s, %(idx)s, %(channel)s, "
                "%(task_path)s, %(type)s, %(value_blob)s)" + tail,
                params,
            )

    def delete_thread(self, thread_id: str) -> None:
        """清掉一个会话的全部 checkpoint 与待回放写入（两张表都要）。"""
        self.ob.execute("DELETE FROM graph_checkpoint_writes WHERE thread_id = %(t)s",
                        {"t": thread_id})
        self.ob.execute("DELETE FROM graph_checkpoint WHERE thread_id = %(t)s", {"t": thread_id})

    # ------------------------------------------------------------------
    # 版本与内部助手
    # ------------------------------------------------------------------
    def get_next_version(self, current: str | None, channel: None) -> str:
        """通道版本号：32 位零填充的单调递增十进制串。

        基类默认对 `str` 版本直接抛 NotImplementedError，而本 saver 声明的是
        `BaseCheckpointSaver[str]`，所以必须覆写。零填充保证字典序 == 数值序
        （框架只要求「单调递增」，比较都在字符串上做）。
        """
        if current is None:
            return "0" * 31 + "1"
        try:
            head = str(int(current.split(".")[0]) + 1)
        except ValueError as exc:
            raise CheckpointStorageError(f"无法解析通道版本号: {current!r}") from exc
        return head.zfill(32)

    @staticmethod
    def _thread(config: RunnableConfig) -> tuple[str, str]:
        c = config.get("configurable") or {}
        thread_id = c.get("thread_id")
        if not thread_id:
            raise CheckpointStorageError("检查点操作需要 config.configurable.thread_id")
        ns = c.get("checkpoint_ns", "")
        if ns:
            # 表 PK 不含 checkpoint_ns（本图无子图，实测 ns 恒为 ''）。
            # 若将来接子图，静默把两个命名空间写进同一行会读出脏数据——宁可炸。
            raise CheckpointStorageError(
                f"OBCheckpointer 只支持根命名空间（checkpoint_ns=''），收到 {ns!r}："
                f"子图需要先给表加 ns 列")
        return str(thread_id), ns

    def _to_tuple(self, row: dict[str, Any]) -> CheckpointTuple:
        thread_id = str(row["thread_id"])
        ckpt_id = str(row["checkpoint_id"])
        checkpoint: Checkpoint = self.serde.loads_typed((str(row["type"]), row["checkpoint"]))
        metadata: CheckpointMetadata = self.serde.loads_typed(
            (str(row["metadata_type"]), row["metadata"]))
        parent_id = row.get("parent_checkpoint_id")
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread_id, "checkpoint_ns": "",
                                     "checkpoint_id": ckpt_id}},
            checkpoint=checkpoint,
            metadata=metadata,
            parent_config=({"configurable": {"thread_id": thread_id, "checkpoint_ns": "",
                                             "checkpoint_id": str(parent_id)}}
                            if parent_id else None),
            pending_writes=self._load_writes(thread_id, ckpt_id),
        )

    def _load_writes(self, thread_id: str, ckpt_id: str) -> PendingWrites:
        rows = self.ob.execute(
            "SELECT task_id, task_path, idx, channel, type, value_blob "
            "FROM graph_checkpoint_writes "
            "WHERE thread_id = %(thread_id)s AND checkpoint_id = %(checkpoint_id)s "
            f"ORDER BY {_WRITES_ORDER}",
            {"thread_id": thread_id, "checkpoint_id": ckpt_id},
        )
        # PendingWrite = (task_id, channel, value)；顺序即 get_tuple 契约要求的 writes_sort_key
        return [(str(r["task_id"]), str(r["channel"]),
                 self.serde.loads_typed((str(r["type"]), r["value_blob"])))
                for r in rows]


def make_checkpointer(backend: str, ob: OBConnection | None = None) -> Any:
    """按开关构造检查点器（架构 §2.2 L5：memory=开发/测试，ob=同库持久化）。

    只在 `graph.py` 的模块级单例处调用一次——绝不能在 build_graph 里新建：
    每次新 checkpointer 等于空 checkpoint，resume 直接 KeyError（本项目踩坑 #8）。
    """
    if backend == "ob":
        if ob is None:
            raise ValueError("checkpoint_backend=ob 需要 OBConnection")
        return OBCheckpointer(ob)
    if backend == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
    raise ValueError(f"未知 checkpoint_backend: {backend}（可选 memory | ob）")


__all__ = ["CheckpointStorageError", "OBCheckpointer", "make_checkpointer"]
