"""MCP Server：把 SkillRegistry 动态暴露为 MCP tools（架构 §5.4）。

传输：Streamable HTTP（MCP 推荐远程传输，HTTP POST + 可选 SSE）。
外部 Agent（评委环境 / 高德 Agent 形态）可直接挂载本 Server，
`tools/list` 即可看到全部 Skill（Pydantic schema 自动导出 JSON Schema）。

启动：
    python -m backend.mcp_server.server          # streamable-http，默认 127.0.0.1:8765
    MCP_HOST=0.0.0.0 MCP_PORT=9000 python -m backend.mcp_server.server
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP

from backend.app import build_services, build_skill_context
from backend.skills.base import SpatialSkill
from backend.skills.registry import SkillRegistry

mcp = FastMCP("geomind-spatial")

_DEFAULT_SESSION = "mcp-default"


def _make_handler(skill: SpatialSkill, ctx: Any) -> Any:
    """按 Skill 的 input_schema **动态生成显式签名的 async handler**。

    为什么不能用 `async def handler(**kwargs)`：FastMCP 从函数签名推断参数模型，
    `**kwargs` 会被当成一个必填字段（实测报 "kwargs Field required"）。

    默认值处理：签名统一 `=None`，函数体过滤掉 None 后再交给 Pydantic ——
    这样 default / default_factory 全部由模型自身处理（若在签名里写
    `f.default!r`，default_factory 字段会生成 PydanticUndefined 字面量而
    NameError，本项目实测踩坑）。
    """
    names = list(skill.input_schema.model_fields.keys())
    sig = ", ".join(f"{n}=None" for n in names)
    pairs = ", ".join(f'"{n}": {n}' for n in names)
    # 用字符串拼接而非 % / .format：生成的源码里本身含大量花括号
    src = (
        "async def _handler(" + sig + "):\n"
        "    _kw = {k: v for k, v in {" + pairs + "}.items() if v is not None}\n"
        "    out = skill.run(skill.input_schema(**_kw), ctx)\n"
        "    return out.model_dump()\n"
    )
    ns: dict[str, Any] = {"skill": skill, "ctx": ctx}
    exec(src, ns)  # noqa: S102 - 受控源码，参数名来自 Pydantic schema
    return ns["_handler"]


def _bind() -> int:
    """discover 全部 Skill 并注册为 MCP tool。返回新注册数（已注册的跳过）。"""
    services = build_services()
    ctx = build_skill_context(_DEFAULT_SESSION, services)
    reg = SkillRegistry.instance()
    reg.discover()
    existing = {t.name for t in mcp._tool_manager.list_tools()} if hasattr(mcp, "_tool_manager") else set()
    added = 0
    for name, skill in reg.skills.items():
        if name in existing:
            continue
        mcp.tool(name=name, description=skill.metadata.description)(_make_handler(skill, ctx))
        added += 1
    return added


def main() -> None:
    n = _bind()
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8765"))
    mcp.settings.host = host
    mcp.settings.port = port
    print(f"[mcp] 本次新注册 {n} 个 Skill；tools/list 应可见 "
          f"{len(SkillRegistry.instance().skills)} 个工具", flush=True)
    print(f"[mcp] Streamable HTTP 监听 http://{host}:{port}/mcp", flush=True)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
