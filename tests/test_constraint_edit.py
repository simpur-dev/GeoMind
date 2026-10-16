"""约束点改闭环测试（任务卡 6，架构 §9.2 / §9.3 / §3.1 / §9.6）。

三层，全部离线（conftest 已强制 fake LLM / rule 意图 / 空高德 key / memory 检查点）：
  · 校验模型：白名单恰四项 + 区间边界 + 陌生键必拒（纯函数，零依赖零 DB）；
  · 图侧：覆盖只碰被点的键、绕过端点的陌生键污染不了 spec、不传覆盖等于写 None
    （节点空转，且新一轮提问不会继承上一轮的点改）；
  · 集成：TestClient + 真 SeekDB——radius 生效并重算、iso_minutes 触发等时圈反哺
    （候选按面筛）、非法值 422 且整份图状态一字不变、改回原值则状态逐字节相同。
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.api.contracts.constraints import ConstraintOverrides
from backend.api.routes import create_app
from backend.brain import graph as graph_mod
from backend.brain.graph import _constraints_node, resume_query, run_query
from backend.skills.base import SkillContext

Q = "帮我在春熙路附近选个咖啡馆位置"
EDITABLE = {"radius_m", "min_rating", "iso_minutes", "travel_mode"}
INTENT = {"slots": {"center": [104.0817, 30.657], "radius_m": 1500, "category_id": 1,
                    "keywords": ["咖啡"], "query_text": "咖啡馆",
                    "iso_minutes": None, "travel_mode": None}}


def bare_ctx() -> SkillContext:
    """纯节点只要 `services.events=None`（emit 静默跳过）就能打。"""
    return SkillContext(session_id="t-edit", state={}, services=SimpleNamespace(events=None))


def spec_of(state: dict[str, Any]) -> dict[str, Any]:
    return _constraints_node(state, bare_ctx())["spec"]      # type: ignore[index]


# ---------------------------------------------------------------------------
# 校验模型（白名单的唯一出处）
# ---------------------------------------------------------------------------
def test_whitelist_is_exactly_the_four_keys():
    assert set(ConstraintOverrides.model_fields) == EDITABLE


@pytest.mark.parametrize("changes", [
    {"radius_m": 99},                       # 下界外
    {"radius_m": 50_001},                   # 上界外（比检索 schema 的 100 km 更严）
    {"min_rating": 5.5}, {"min_rating": -0.1},
    {"iso_minutes": 0}, {"iso_minutes": 121},
    {"travel_mode": "火箭"}, {"travel_mode": "bicycling"},   # 高德无骑行档（卡 1 实测）
    {"keywords": ["咖啡"]},                  # 禁止开放点改的四类
    {"category_id": 1}, {"time_bucket": "workday_noon"},
    {"rent": 80000},                         # §9.8 curl 示例里的扁平键，同样拒
])
def test_out_of_whitelist_or_out_of_range_is_rejected(changes: dict[str, Any]):
    with pytest.raises(ValidationError):
        ConstraintOverrides.model_validate(changes)


@pytest.mark.parametrize("changes", [
    {"radius_m": 100}, {"radius_m": 50_000},                 # 边界本身合法（闭区间）
    {"min_rating": 0}, {"min_rating": 4.5},
    {"iso_minutes": 1}, {"iso_minutes": 120},
    {"travel_mode": "walking"}, {"travel_mode": "driving"},
])
def test_legal_changes_keep_untouched_keys_as_none(changes: dict[str, Any]):
    dumped = ConstraintOverrides.model_validate(changes).model_dump(exclude_none=True)
    assert dumped == changes                              # 没被点的键不会冒出来


def test_multiple_keys_and_empty_changes():
    ov = ConstraintOverrides.model_validate({"iso_minutes": 20, "travel_mode": "walking",
                                             "radius_m": 3000, "min_rating": 4.5})
    assert ov.model_dump(exclude_none=True) == {"radius_m": 3000, "min_rating": 4.5,
                                                "iso_minutes": 20, "travel_mode": "walking"}
    assert ConstraintOverrides.model_validate({}).model_dump(exclude_none=True) == {}


# ---------------------------------------------------------------------------
# 图侧：覆盖的生效范围
# ---------------------------------------------------------------------------
def test_overrides_only_touch_the_named_keys():
    base = spec_of({"intent": INTENT})
    got = spec_of({"intent": INTENT, "spec_overrides": {"radius_m": 3000, "iso_minutes": 20}})
    assert set(got) == set(base)                                   # 键集合不扩张
    assert {k: v for k, v in got.items() if v != base[k]} == {"radius_m": 3000, "iso_minutes": 20}


def test_stranger_keys_cannot_pollute_spec_even_though_they_are_spec_keys():
    base = spec_of({"intent": INTENT})
    got = spec_of({"intent": INTENT, "spec_overrides": {
        "center": [1.0, 2.0], "keywords": ["火箭"], "time_bucket": "workday_noon",
        "category_id": 9, "rent": 80000, "radius_m": 3000}})
    assert got["radius_m"] == 3000                                 # 白名单内的照常生效
    for k in ("center", "keywords", "time_bucket", "category_id"):  # 在 spec 里但不在名单里
        assert got[k] == base[k], f"{k} 被陌生覆盖污染了"
    assert "rent" not in got


def test_no_overrides_leaves_the_spec_byte_identical():
    base = spec_of({"intent": INTENT})
    # run_query 每轮都写这个通道：不传即 None，节点必须与「根本没有该键」完全同路
    for empty in ({}, None):
        got = spec_of({"intent": INTENT, "spec_overrides": empty})
        assert got == base
        assert json.dumps(got, sort_keys=True) == json.dumps(base, sort_keys=True)


def test_run_query_writes_the_channel_every_round_but_resume_keeps_it(monkeypatch: pytest.MonkeyPatch):
    """run_query 不传覆盖时写入 None（新一轮不被上一轮的点改污染）；
    resume 不带覆盖时构造的 Command 与改动前一致（update=None → 沿用同轮已有覆盖）。"""
    seen: list[Any] = []

    class SpyApp:
        def invoke(self, inp: Any, config: Any) -> dict[str, Any]:
            seen.append(inp)
            return {}

    monkeypatch.setattr(graph_mod, "build_graph", lambda ctx: SpyApp())

    run_query(Q, bare_ctx(), thread_id="t-spy")
    run_query(Q, bare_ctx(), thread_id="t-spy", spec_overrides={"radius_m": 3000})
    assert seen[0]["spec_overrides"] is None
    assert seen[1]["spec_overrides"] == {"radius_m": 3000}

    resume_query("春熙路", bare_ctx(), thread_id="t-spy")
    assert seen[2].resume == "春熙路" and seen[2].update is None
    resume_query("春熙路", bare_ctx(), thread_id="t-spy", spec_overrides={"min_rating": 4.5})
    assert seen[3].update == {"spec_overrides": {"min_rating": 4.5}}


# ---------------------------------------------------------------------------
# 集成（TestClient + 真 SeekDB；无 key → 等时圈走正圆降级）
# ---------------------------------------------------------------------------
@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


def new_session(client: TestClient) -> str:
    return client.post("/api/sessions", json={"scene": "site_selection"}).json()["session_id"]


def graph_state(client: TestClient, sid: str) -> dict[str, Any]:
    return client.get(f"/api/sessions/{sid}").json()["graph_state"]


def edit(client: TestClient, sid: str, changes: Any, query: Any = Q) -> Any:
    return client.post(f"/api/sessions/{sid}/feedback",
                       json={"type": "constraint_edit",
                             "payload": {"changes": changes, "query": query}})


def test_edit_radius_rerounds_with_the_new_constraint(client, db_ok):
    """①radius_m 点改成 3000 → 200、spec 生效、候选按新半径重算。"""
    sid = new_session(client)
    assert not client.post(f"/api/sessions/{sid}/chat", json={"query": Q}).json()["interrupted"]
    st = graph_state(client, sid)
    assert st["spec"]["radius_m"] == 1500
    before = {c["poi_id"] for c in st["candidates"]}
    assert all(c["dist_m"] <= 1500 for c in st["candidates"])       # 实测最大 1280 m

    r = edit(client, sid, {"radius_m": 3000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True and body["changes"] == {"radius_m": 3000}

    st2 = graph_state(client, sid)
    assert st2["spec"]["radius_m"] == 3000
    assert body["result"]["candidates"] == st2["candidates"], "返回体就是重算那一轮的状态"
    after = {c["poi_id"] for c in st2["candidates"]}
    assert after != before, "半径从 1500 换到 3000，候选集必须变（否则只是嘴上重算）"
    assert max(c["dist_m"] for c in st2["candidates"]) > 1500       # 实测 2715 m：圆真的放开了
    assert all(c["dist_m"] <= 3000 for c in st2["candidates"])


def test_plain_chat_after_edit_does_not_inherit_the_override(client, db_ok):
    """点改只管「重算那一轮」：随后一次新提问按意图重新装配，不带着旧的 3000。"""
    sid = new_session(client)
    client.post(f"/api/sessions/{sid}/chat", json={"query": Q})
    assert edit(client, sid, {"radius_m": 3000}).status_code == 200
    assert graph_state(client, sid)["spec"]["radius_m"] == 3000

    client.post(f"/api/sessions/{sid}/chat", json={"query": Q})
    st = graph_state(client, sid)
    assert st["spec"]["radius_m"] == 1500, "新一轮不该继承上一轮的 chip 覆盖"
    assert st.get("spec_overrides") is None


def test_edit_iso_minutes_feeds_candidate_retrieval(client, db_ok, services):
    """②iso_minutes 点改成 20 → 卡 5 的显式反哺语义自动生效（本卡零额外代码）。"""
    sid = new_session(client)
    client.post(f"/api/sessions/{sid}/chat", json={"query": Q})
    assert graph_state(client, sid)["spec"]["iso_minutes"] is None   # 起点：没给时间预算

    r = edit(client, sid, {"iso_minutes": 20})
    assert r.status_code == 200, r.text
    st = graph_state(client, sid)
    assert st["spec"]["iso_minutes"] == 20
    assert st["isochrone"]["minutes"] == 20 and st["isochrone"]["polygon_wkt"].startswith("POLYGON((")
    assert "iso_filter_skip" not in st, "面约束该进检索，不该回落"
    # 圆约束确实让位：面约束分支的 SELECT 不产 dist_m（实测整列 None）
    assert st["spec"]["radius_m"] == 1500 and all(c["dist_m"] is None for c in st["candidates"])

    ids = [c["poi_id"] for c in st["candidates"]]
    ph = ", ".join(f"%(i{i})s" for i in range(len(ids)))
    params: dict[str, Any] = {f"i{i}": v for i, v in enumerate(ids)}
    params["wkt"] = st["isochrone"]["polygon_wkt"]
    outside = services.ob.execute(
        f"SELECT COUNT(*) AS n FROM dim_poi WHERE poi_id IN ({ph}) "
        "AND NOT ST_Contains(ST_GeomFromText(%(wkt)s), location)", params)[0]["n"]
    assert int(outside) == 0, f"{outside} 个候选落在 20 分钟圈外 → chip 没接进闭环"


@pytest.mark.parametrize("changes", [
    {"radius_m": 999_999}, {"travel_mode": "火箭"}, {"keywords": ["咖啡"], "radius_m": 3000},
    {"center": [104.0, 30.6]}, {}, "radius=3000", None,
])
def test_illegal_edit_is_422_and_state_untouched(client, db_ok, changes: Any):
    """③非法值 / 白名单外键 / 载荷形态不对 → 422，且图状态一字不变（校验在 invoke 之前）。"""
    sid = new_session(client)
    client.post(f"/api/sessions/{sid}/chat", json={"query": Q})
    before = json.dumps(graph_state(client, sid), sort_keys=True, ensure_ascii=False)

    r = edit(client, sid, changes)
    assert r.status_code == 422, f"{changes} 竟被接受：{r.status_code} {r.text[:200]}"
    assert json.dumps(graph_state(client, sid), sort_keys=True, ensure_ascii=False) == before


def test_edit_without_query_is_422(client):
    """重跑必须有原文 query（扁平 payload 给不出）——不碰库，所以无需 db_ok。"""
    assert edit(client, "no-such-session", {"radius_m": 3000}, query="").status_code == 422
    r = client.post("/api/sessions/no-such-session/feedback",
                    json={"type": "constraint_edit", "payload": {"changes": {"radius_m": 3000}}})
    assert r.status_code == 422


def test_unknown_feedback_type_still_400(client):
    """constraint_edit 进名单后，兜底分支仍按原样拒未知类型（§9.3 契约没被放宽）。"""
    r = client.post("/api/sessions/no-such-session/feedback",
                    json={"type": "drag_marker", "payload": {}})
    assert r.status_code == 400 and "未知 feedback 类型" in r.text


def test_noop_edit_leaves_the_whole_state_byte_identical(client, db_ok):
    """④把 chip 改回「本来就是它」的值 → 整份图状态与点改前逐字节相同。

    这是「不带 overrides 的行为不变」的用户可见版本：新注入的只有 `spec_overrides`
    这一个通道键，其余（候选/聚合/决策/Map Spec/等时圈）一律不动。
    """
    sid = new_session(client)
    client.post(f"/api/sessions/{sid}/chat", json={"query": Q})
    a = graph_state(client, sid)
    assert a.get("spec_overrides") is None            # 新一轮没带覆盖 → 通道就是 None

    assert edit(client, sid, {"radius_m": 1500}).status_code == 200
    b = graph_state(client, sid)
    assert b.pop("spec_overrides") == {"radius_m": 1500}
    a.pop("spec_overrides", None)
    assert json.dumps(b, sort_keys=True, ensure_ascii=False) \
        == json.dumps(a, sort_keys=True, ensure_ascii=False)


def test_resume_still_works_with_the_new_default_kwarg(client, db_ok):
    """resume 签名向后兼容：既有 HITL 链路（不带任何新参数）照旧能续跑。"""
    sid = new_session(client)
    first = client.post(f"/api/sessions/{sid}/chat",
                        json={"query": "找个安静能自习的咖啡馆"}).json()
    assert first["interrupted"], "低置信度应停在追问"
    second = client.post(f"/api/sessions/{sid}/resume", json={"answer": "春熙路"}).json()
    assert not second["interrupted"] and second["candidates"]
    assert graph_state(client, sid)["spec"]["center"] == [104.0817, 30.657]
