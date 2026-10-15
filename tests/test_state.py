"""Spatial State 测试（设计文档 §8 第 3 行：局部重算与版本）。"""
from __future__ import annotations

import pytest

from backend.state.models import SpatialState, StepNode
from backend.state.repository import StateNotFoundError, StateRepository


def _state(sid: str = "t-state") -> SpatialState:
    return SpatialState(
        session_id=sid, scene="site_selection", version=1,
        slots={"category_id": 1},
        steps=[
            StepNode(id="s1_candidates", skill="multi_modal_search", status="done"),
            StepNode(id="s2_spatial_join", skill="spatial_join_agg", status="done",
                     depends_on=["s1_candidates"]),
            StepNode(id="s3_score", skill="score_and_rank", status="done",
                     depends_on=["s2_spatial_join"]),
        ],
    )


class TestDependencyGraph:
    def test_mark_stale_reverse_closure(self):
        st = _state()
        affected = st.mark_stale("s1_candidates")
        assert affected == ["s1_candidates", "s2_spatial_join", "s3_score"]
        assert all(s.status == "stale" for s in st.steps)

    def test_mark_stale_leaf_only(self):
        st = _state()
        affected = st.mark_stale("s3_score")
        assert affected == ["s3_score"]

    def test_replan_topological_order(self):
        st = _state()
        st.mark_stale("s1_candidates")
        assert st.replan() == ["s1_candidates", "s2_spatial_join", "s3_score"]

    def test_replan_empty_when_clean(self):
        assert _state().replan() == []

    def test_replan_ignores_done_deps(self):
        st = _state()
        st.mark_stale("s2_spatial_join")   # s1 仍 done，不应阻塞
        assert st.replan() == ["s2_spatial_join", "s3_score"]

    def test_cycle_defensive(self):
        st = SpatialState(session_id="cyc", scene="x",
                          steps=[StepNode(id="a", skill="a", depends_on=["b"]),
                                 StepNode(id="b", skill="b", depends_on=["a"])])
        st.mark_stale("a")
        assert set(st.replan()) == {"a", "b"}


class TestRepository:
    def test_save_load_roundtrip(self, services, db_ok):
        repo = StateRepository(services.ob)
        sid = "pytest-state-rt"
        try:
            repo.create(sid, "site_selection", {"category_id": 1})
            st = repo.load(sid)
            assert st.session_id == sid and st.version == 1
            st.mark_stale("s1_candidates") if st.steps else None
            saved = repo.save(st)
            assert saved.version == 2
            assert repo.load(sid).version == 2
        finally:
            services.ob.execute("DELETE FROM scene_solution WHERE session_id=%(s)s", {"s": sid})

    def test_load_missing_raises(self, services, db_ok):
        repo = StateRepository(services.ob)
        with pytest.raises(StateNotFoundError):
            repo.load("no-such-session-xyz")

    def test_mark_stale_persists(self, services, db_ok):
        repo = StateRepository(services.ob)
        sid = "pytest-state-stale"
        try:
            repo.create(sid, "site_selection", {})
            st = repo.load(sid)
            st.steps = [StepNode(id="s1", skill="a", status="done"),
                        StepNode(id="s2", skill="b", status="done", depends_on=["s1"])]
            repo.save(st)
            affected = repo.mark_stale(sid, "s1")
            assert affected == ["s1", "s2"]
            assert repo.replan(sid) == ["s1", "s2"]
        finally:
            services.ob.execute("DELETE FROM scene_solution WHERE session_id=%(s)s", {"s": sid})
