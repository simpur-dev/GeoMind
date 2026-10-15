"""REST / SSE 路由（架构 §9.3 前后端交互契约的最小实现）。

通道平等观：Web / CLI / HTTP / MCP 挂在同一套内核契约上（架构 §9.1）。
本轮只实现契约，不含任何业务页面。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend.app import build_services, build_skill_context
from backend.brain.graph import resume_query, run_query
from backend.skills.registry import SkillRegistry
from backend.state.repository import StateNotFoundError, StateRepository


class CreateSessionIn(BaseModel):
    scene: str = "site_selection"
    slots: dict[str, Any] = Field(default_factory=dict)


class ChatIn(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    gesture: dict[str, Any] | None = None


class ResumeIn(BaseModel):
    answer: str = Field(min_length=1, max_length=200)


class FeedbackIn(BaseModel):
    node_id: str = Field(description="被反馈影响的节点 id，如 candidates")
    action: str = Field(default="reject", description="adopt | reject")


class ChatOut(BaseModel):
    session_id: str
    interrupted: bool = False
    question: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    aggs: list[dict[str, Any]] = Field(default_factory=list)
    decision: dict[str, Any] = Field(default_factory=dict)
    map_spec: dict[str, Any] = Field(default_factory=dict)


def create_app() -> FastAPI:
    app = FastAPI(title="GeoMind", version="0.1.0",
                  description="空间智能体内核：同库五模，一条 SQL")
    router = APIRouter(prefix="/api")

    services = build_services()
    repo = StateRepository(services.ob)
    SkillRegistry.instance().discover()

    def _ctx(session_id: str) -> Any:
        return build_skill_context(session_id, services)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"ok": services.ob.ping(), "skills": len(SkillRegistry.instance().skills)}

    @router.get("/skills")
    def list_skills() -> dict[str, Any]:
        return {"skills": SkillRegistry.instance().describe_all()}

    @router.post("/sessions")
    def create_session(body: CreateSessionIn) -> dict[str, Any]:
        sid = uuid.uuid4().hex[:16]
        state = repo.create(sid, body.scene, body.slots)
        return {"session_id": sid, "version": state.version}

    @router.get("/sessions/{sid}/state")
    def get_state(sid: str) -> dict[str, Any]:
        try:
            return repo.load(sid).model_dump(mode="json")
        except StateNotFoundError as exc:
            raise HTTPException(404, f"会话不存在: {sid}") from exc

    @router.post("/sessions/{sid}/chat", response_model=ChatOut)
    def chat(sid: str, body: ChatIn) -> ChatOut:
        r = run_query(body.query, _ctx(sid), gesture=body.gesture, thread_id=sid)
        return _chat_out(sid, r)

    @router.post("/sessions/{sid}/resume", response_model=ChatOut)
    def resume(sid: str, body: ResumeIn) -> ChatOut:
        r = resume_query(body.answer, _ctx(sid), thread_id=sid)
        return _chat_out(sid, r)

    @router.post("/sessions/{sid}/feedback")
    def feedback(sid: str, body: FeedbackIn) -> dict[str, Any]:
        try:
            affected = repo.mark_stale(sid, body.node_id)
        except StateNotFoundError as exc:
            raise HTTPException(404, f"会话不存在: {sid}") from exc
        return {"stale": affected, "replan": repo.replan(sid)}

    @router.get("/sessions/{sid}/events")
    async def events(sid: str) -> Any:
        """SSE：token / tool.start / tool.end / layer.update / state.version（架构 §9.3）。"""
        from fastapi.responses import StreamingResponse

        async def gen() -> AsyncIterator[str]:
            yield _sse({"type": "state.version", "session_id": sid, "version": 1})
            yield _sse({"type": "token", "text": "GeoMind 内核就绪。"})
            yield _sse({"type": "done"})

        return StreamingResponse(gen(), media_type="text/event-stream")

    app.include_router(router)
    return app


def _chat_out(sid: str, r: dict[str, Any]) -> ChatOut:
    if r.get("interrupted"):
        return ChatOut(session_id=sid, interrupted=True, question=r.get("question"))
    st = r.get("state", {})
    return ChatOut(
        session_id=sid, interrupted=False,
        candidates=st.get("candidates", []), aggs=st.get("aggs", []),
        decision=st.get("decision", {}), map_spec=st.get("map_spec", {}),
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


__all__ = ["create_app", "asyncio"]
