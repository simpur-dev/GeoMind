"""MCP Server 测试（设计文档 §8 第 5 行：tools/list 可见 + inputSchema 合法）。"""
from __future__ import annotations

import pytest

from backend.mcp_server import server as mcp_server
from backend.skills.registry import SkillRegistry

pytestmark = pytest.mark.asyncio


class TestMCPServer:
    async def test_bind_registers_all_tools(self, services):
        n = mcp_server._bind()
        assert n >= 0
        tools = await mcp_server.mcp.list_tools()
        names = {t.name for t in tools}
        assert {"multi_modal_search", "spatial_join_agg", "traffic_grid_agg",
                "knowledge_search", "poi_detail", "site_selection"} <= names
        assert len(SkillRegistry.instance().skills) >= 6

    async def test_tools_have_input_schema(self, services):
        mcp_server._bind()
        tools = await mcp_server.mcp.list_tools()
        for t in tools:
            assert t.inputSchema, f"{t.name} 缺 inputSchema"
            assert t.inputSchema.get("type") == "object"
            assert t.description

    async def test_call_multi_modal_search(self, services, db_ok):
        """tools/call 走通一个工具（对拍 Skill 直接调用）。"""
        mcp_server._bind()
        result = await mcp_server.mcp.call_tool("multi_modal_search", {
            "center": [104.0817, 30.6570], "radius_m": 1500, "category_id": 1,
            "keywords": ["咖啡"], "query_text": "安静可自习的咖啡馆", "limit": 5,
        })
        # FastMCP 返回 (content, structured) 或 content 列表
        payload = result[1] if isinstance(result, tuple) else result
        if isinstance(payload, dict):
            cands = payload.get("candidates", [])
        else:
            import json
            cands = json.loads(payload[0].text).get("candidates", []) if payload else []
        assert cands, "tools/call 应返回候选"
