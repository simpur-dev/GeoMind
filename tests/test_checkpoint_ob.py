"""OBCheckpointer 单测与集成（任务卡 3，架构 §2.2 L5「同库持久化」）。

约定：
  · 所有用例都打真库（db_ok 不可达即 skip），但**不打任何外部服务**——
    interrupt/resume 集成用的图里 reachability 走无 key 正圆降级（conftest 保证）；
  · 每条用例一个独立 thread_id，teardown 从库里删干净（不留脏行给下一条）；
  · "恢复"一律用**新实例**断言：同进程新建对象已等价于换进程（表才是状态载体），
    真正的跨进程证据在《开发进度.md》时间线里的 HTTP 验收。
"""
from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.base.id import uuid6

from backend.brain import graph as graph_mod
from backend.db.checkpoint_ob import CheckpointStorageError, OBCheckpointer
from backend.db.connection import OBConnection, OBError

THREAD_NS = {"checkpoint_ns": ""}


def new_id() -> str:
    """checkpoint_id 的口径：str(uuid6)。

    注意 `uuid6()` 本身返回 UUID 对象（实测），而 langgraph 与库里存的都是字符串，
    测试直接拿对象会在 parent_config 比对上炸出「UUID != str」。
    """
    return str(uuid6())


def cfg(thread: str, ckpt_id: str | None = None) -> dict[str, Any]:
    c: dict[str, Any] = {"thread_id": thread, **THREAD_NS}
    if ckpt_id:
        c["checkpoint_id"] = ckpt_id
    return {"configurable": c}


def make_ckpt(thread: str, ckpt_id: str, parent: str | None, values: dict[str, Any],
              step: int = 1) -> dict[str, Any]:
    """自己搭最小 checkpoint（字段口径对齐实测到的 Checkpoint 形态，不依赖库内构造器）。"""
    return {
        "v": 1,
        "id": ckpt_id,
        "ts": "2026-10-16T12:00:00.000000+00:00",
        "channel_values": values,
        "channel_versions": {k: f"{i + 1:032d}" for i, k in enumerate(values)},
        "versions_seen": {t: {} for t in ("intent", "constraints")},
        "updated_channels": list(values),
    }


@pytest.fixture()
def saver(services: Any) -> OBCheckpointer:
    cp = OBCheckpointer(services.ob)
    assert isinstance(services.ob, OBConnection)
    return cp


@pytest.fixture()
def thread(services: Any) -> Any:
    """每条用例一个 thread_id，用完从两张表里删干净（防止串行污染）。"""
    tid = f"t-ckpt-{uuid6()}".replace("-", "_")[:32]
    yield tid
    services.ob.execute("DELETE FROM graph_checkpoint_writes WHERE thread_id = %(t)s", {"t": tid})
    services.ob.execute("DELETE FROM graph_checkpoint WHERE thread_id = %(t)s", {"t": tid})


# ---------------------------------------------------------------------------
# 纯逻辑（不需要库）
# ---------------------------------------------------------------------------
def test_get_next_version_is_monotonic_string(saver: OBCheckpointer):
    """基类对 str 版本抛 NotImplementedError，本类必须自己给出单调串。"""
    v1 = saver.get_next_version(None, None)
    v2 = saver.get_next_version(v1, None)
    v3 = saver.get_next_version(v2, None)
    assert v1 < v2 < v3 and len(v1) == 32
    assert saver.get_next_version("0" * 31 + "9", None) == "0" * 30 + "10"


def test_get_next_version_rejects_garbage(saver: OBCheckpointer):
    with pytest.raises(CheckpointStorageError, match="版本号"):
        saver.get_next_version("not-a-version", None)


def test_thread_id_is_required(saver: OBCheckpointer):
    with pytest.raises(CheckpointStorageError, match="thread_id"):
        saver.get_tuple({"configurable": dict(THREAD_NS)})


def test_subgraph_namespace_is_refused_not_silently_collided(saver: OBCheckpointer, thread: str):
    """表 PK 不含 checkpoint_ns（本图无子图）：非空 ns 必须炸，不能静默写进同一行。"""
    bad = {"configurable": {"thread_id": thread, "checkpoint_ns": "sub:abc"}}
    with pytest.raises(CheckpointStorageError, match="子图"):
        saver.get_tuple(bad)
    with pytest.raises(CheckpointStorageError, match="子图"):
        saver.put(bad, make_ckpt(thread, new_id(), None, {"a": 1}), {"step": 1, "source": "loop"}, {})


# ---------------------------------------------------------------------------
# 真库 round-trip
# ---------------------------------------------------------------------------
def test_put_then_get_tuple_from_new_instance(services: Any, saver: OBCheckpointer, thread: str):
    """验收①：实例 A 写 → **新建实例 B** 读回等价 checkpoint（模拟进程重启）。"""
    cid = new_id()
    ckpt = make_ckpt(thread, cid, None, {"query": "春熙路咖啡", "candidates": [{"poi_id": 1}],
                                          "spec": {"center": [104.0817, 30.657], "radius_m": 1500}})
    meta: dict[str, Any] = {"step": 0, "source": "input", "parents": {}}
    returned = saver.put(cfg(thread), ckpt, meta, {})   # type: ignore[arg-type]
    assert returned["configurable"]["checkpoint_id"] == cid

    fresh = OBCheckpointer(services.ob)                 # 新实例 = 换进程的最小等价模拟
    got = fresh.get_tuple(cfg(thread))
    assert got is not None and got.checkpoint["id"] == cid
    assert got.checkpoint["channel_values"] == ckpt["channel_values"]
    assert got.metadata["step"] == 0 and got.metadata["source"] == "input"
    assert got.pending_writes == [] and got.parent_config is None
    assert got.config["configurable"]["thread_id"] == thread


def test_missing_checkpoint_returns_none(saver: OBCheckpointer):
    missing = cfg("t-ckpt-does-not-exist")
    assert saver.get_tuple(missing) is None
    assert saver.get(missing) is None          # get 由基类委派 get_tuple


def test_channel_absent_from_snapshot_is_not_invented(saver: OBCheckpointer, thread: str):
    """整份 blob 的保真检查：没写进 channel_values 的键，读回也不许冒出来。"""
    ck1 = make_ckpt(thread, new_id(), None, {"spec": {"radius_m": 1500}})
    saver.put(cfg(thread), ck1, {"step": 1, "source": "loop", "parents": {}}, {})  # type: ignore[arg-type]
    got = saver.get_tuple(cfg(thread))
    assert got is not None
    assert "candidates" not in got.checkpoint["channel_values"]
    assert got.checkpoint["channel_values"]["spec"] == {"radius_m": 1500}


def test_parent_chain_list_desc_before_and_limit(saver: OBCheckpointer, thread: str):
    """验收③：list 倒序 + before + limit；parent_config 逐跳指回前驱。"""
    ids = [new_id() for _ in range(3)]
    parent: str | None = None
    for i, cid in enumerate(ids):
        saver.put(cfg(thread, parent), make_ckpt(thread, cid, parent, {"step": i}),
                  {"step": i, "source": "loop", "parents": {}}, {})   # type: ignore[arg-type]
        parent = cid
    assert ids == sorted(ids)                       # uuid6 单调，前提成立才继续

    got = list(saver.list(cfg(thread)))
    assert [t.checkpoint["id"] for t in got] == list(reversed(ids))
    assert got[0].parent_config["configurable"]["checkpoint_id"] == ids[1]      # type: ignore[index]
    assert got[-1].parent_config is None                                      # 根节点无前驱

    only = list(saver.list(cfg(thread), limit=1))
    assert [t.checkpoint["id"] for t in only] == [ids[2]]
    before = list(saver.list(cfg(thread), before=cfg(thread, ids[2])))
    assert [t.checkpoint["id"] for t in before] == [ids[1], ids[0]]


def test_list_metadata_filter_hits_and_misses(saver: OBCheckpointer, thread: str):
    cid = new_id()
    saver.put(cfg(thread), make_ckpt(thread, cid, None, {"a": 1}),
              {"step": 3, "source": "loop", "parents": {}}, {})           # type: ignore[arg-type]
    assert len(list(saver.list(cfg(thread), filter={"step": 3}))) == 1
    assert list(saver.list(cfg(thread), filter={"step": 99})) == []
    assert len(list(saver.list(cfg(thread), filter={"source": "loop"}))) == 1


def test_put_writes_pending_order_and_values(saver: OBCheckpointer, thread: str):
    """验收②：put_writes 后 get_tuple 带 pending_writes，且按 writes_sort_key 有序。"""
    cid = new_id()
    saver.put(cfg(thread), make_ckpt(thread, cid, None, {"query": "q"}),
              {"step": 1, "source": "loop", "parents": {}}, {})           # type: ignore[arg-type]
    c = cfg(thread, cid)
    saver.put_writes(c, [("aggs", [1, 2]), ("isochrone", {"ok": True})], "task-b",
                     task_path="~__pregel_pull, spatial_join")
    saver.put_writes(c, [("__resume__", "春熙路")], "00000000-0000-0000-0000-000000000000")

    got = saver.get_tuple(c)
    assert got is not None
    pend = got.pending_writes or []
    assert [p[1] for p in pend] == ["__resume__", "aggs", "isochrone"]   # 空 task_path 排在前
    by_channel = {p[1]: p[2] for p in pend}
    assert by_channel["isochrone"] == {"ok": True}
    assert pend[0][0] == "00000000-0000-0000-0000-000000000000"


def test_special_channel_overwrites_and_normal_channel_skips(saver: OBCheckpointer, thread: str):
    """特殊通道（负索引）必须可覆盖，普通通道（正索引）重放先到先得。"""
    cid = new_id()
    saver.put(cfg(thread), make_ckpt(thread, cid, None, {"q": 1}),
              {"step": 1, "source": "loop", "parents": {}}, {})           # type: ignore[arg-type]
    c = cfg(thread, cid)
    saver.put_writes(c, [("__interrupt__", {"question": "第一版"})], "task-a")
    saver.put_writes(c, [("__interrupt__", {"question": "第二版"})], "task-a")
    saver.put_writes(c, [("candidates", [1])], "task-a")
    saver.put_writes(c, [("candidates", [2])], "task-a")

    got = saver.get_tuple(c)
    assert got is not None
    pend = {p[1]: p[2] for p in (got.pending_writes or [])}
    assert pend["__interrupt__"] == {"question": "第二版"}
    assert pend["candidates"] == [1]


def test_delete_thread_leaves_zero_rows(saver: OBCheckpointer, services: Any, thread: str):
    """验收④：delete_thread 后**从库里读回计数**必须是 0（不看返回值脸色）。"""
    cid = new_id()
    saver.put(cfg(thread), make_ckpt(thread, cid, None, {"a": 1}),
              {"step": 1, "source": "loop", "parents": {}}, {})           # type: ignore[arg-type]
    saver.put_writes(cfg(thread, cid), [("spec", {"x": 1})], "task-a")
    assert saver.get_tuple(cfg(thread)) is not None

    saver.delete_thread(thread)
    for table in ("graph_checkpoint", "graph_checkpoint_writes"):
        n = services.ob.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE thread_id = %(t)s",
                                 {"t": thread})[0]["n"]
        assert int(n) == 0, f"{table} 残留 {n} 行"
    assert saver.get_tuple(cfg(thread)) is None


def test_binary_blob_survives_roundtrip_byte_for_byte(saver: OBCheckpointer, services: Any,
                                                     thread: str):
    """LONGBLOB 不许吃字节：原始 blob 与库里读回的 bytes 必须等长且类型正确。"""
    cid = new_id()
    ckpt = make_ckpt(thread, cid, None, {"isochrone": {"polygon_wkt": "POLYGON((1 2, 2 3))"}})
    saver.put(cfg(thread), ckpt, {"step": 1, "source": "loop", "parents": {}}, {})  # type: ignore[arg-type]
    _tag, raw = saver.serde.dumps_typed(ckpt)
    row = services.ob.execute(
        "SELECT checkpoint, LENGTH(checkpoint) AS n FROM graph_checkpoint "
        "WHERE thread_id = %(t)s AND checkpoint_id = %(c)s", {"t": thread, "c": cid})[0]
    assert isinstance(row["checkpoint"], bytes)
    assert row["checkpoint"] == raw and int(row["n"]) == len(raw)


# ---------------------------------------------------------------------------
# 跨实例恢复：真图 interrupt → 换 checkpointer 实例 → resume
# ---------------------------------------------------------------------------
def test_graph_interrupt_resumes_on_fresh_checkpointer(services: Any, thread: str,
                                                      monkeypatch: pytest.MonkeyPatch):
    """§2.2 L5 的卖点：追问到一半换后端实例（等价换进程）还能续跑出候选。"""
    from backend.app import build_skill_context
    from backend.brain.graph import resume_query, run_query

    ctx = build_skill_context(thread, services)
    monkeypatch.setattr(graph_mod, "_CHECKPOINTER", OBCheckpointer(services.ob))

    r1 = run_query("找个安静能自习的咖啡馆", ctx, thread_id=thread)
    assert r1["interrupted"], "无地名应触发 HITL 追问"
    assert r1["question"]["question"]

    # 模拟进程重启：全新 OBCheckpointer 实例（表才是状态载体），同一 thread 续跑
    revived = OBCheckpointer(services.ob)
    monkeypatch.setattr(graph_mod, "_CHECKPOINTER", revived)
    r2 = resume_query("春熙路", ctx, thread_id=thread)

    assert not r2["interrupted"]
    st = r2["state"]
    assert st["candidates"], "resume 后应产出候选"
    assert st["isochrone"]["degraded"] is True          # 无 key 正圆降级（铁律 6）
    # 注意：spec/intent 是 resume 后在**本进程内**重算的节点产物，不能拿来证明反序列化保真；
    # 真正的序列化容器语义由 test_serde_fidelity_* 钉住。这里只验恢复结果正确。
    assert st["intent"]["slots"]["center_name"] == "春熙路"
    assert tuple(st["spec"]["center"]) == pytest.approx((104.0817, 30.657))
    assert st["map_spec"]["layers"], "跨实例恢复后应重新产出图层声明"
    # 清场交给 thread fixture（它会从两张表读回并删除该 thread 的行）


def test_serde_fidelity_documents_container_types(saver: OBCheckpointer, services: Any,
                                                  thread: str):
    """把序列化容器语义钉成事实（实测）：tuple 降级成 list，dict/str/float/int 原样。

    为什么值得一条用例：跨进程恢复后 `spec["center"]` 这类值若是 list，下游
    `tuple[float, float]` 的 Pydantic 入参会强制转回 tuple（`services` 层的
    SpatialConstraint 就这么做），所以「降级」不影响正确性——但它是**行为**，
    写在断言里比写在注释里可靠。
    """
    cid = new_id()
    ckpt = make_ckpt(thread, cid, None, {
        "center_tuple": (104.0817, 30.657), "spec": {"radius_m": 1500},
        "note": "春熙路", "rating": 4.5, "n": 10,
    })
    saver.put(cfg(thread), ckpt, {"step": 1, "source": "loop", "parents": {}}, {})  # type: ignore[arg-type]
    fresh = OBCheckpointer(services.ob)
    values = (fresh.get_tuple(cfg(thread))).checkpoint["channel_values"]  # type: ignore[union-attr]
    assert values["center_tuple"] == [104.0817, 30.657]
    assert isinstance(values["center_tuple"], list)
    assert values["spec"] == {"radius_m": 1500} and values["note"] == "春熙路"
    assert values["rating"] == 4.5 and values["n"] == 10


def test_write_failure_is_not_silently_swallowed(thread: str):
    """checkpoint 丢失比请求失败更危险：库层报错必须原样抛出（不降级、不吞）。"""
    class ExplodingOb:
        def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
            raise OBError("模拟库不可达")

    cp = OBCheckpointer(ExplodingOb())  # type: ignore[arg-type]
    with pytest.raises(OBError, match="模拟库不可达"):
        cp.put(cfg(thread), make_ckpt(thread, new_id(), None, {"a": 1}),
               {"step": 1, "source": "loop", "parents": {}}, {})   # type: ignore[arg-type]
    with pytest.raises(OBError, match="模拟库不可达"):
        cp.get_tuple(cfg(thread))
