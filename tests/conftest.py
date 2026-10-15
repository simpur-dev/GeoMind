"""pytest 共享 fixtures。

数据库相关测试依赖本地 SeekDB（127.0.0.1:2881）；不可达时自动 skip，
保证 CI/无库环境下纯逻辑测试仍可运行。
"""
from __future__ import annotations

import pytest

from backend.app import build_services, build_skill_context
from backend.skills.registry import SkillRegistry


@pytest.fixture(scope="session")
def services():
    return build_services()


@pytest.fixture(scope="session")
def skill_ctx(services):
    return build_skill_context("pytest-session", services)


@pytest.fixture(scope="session")
def db_ok(services):
    if not services.ob.ping():
        pytest.skip("SeekDB 不可达（127.0.0.1:2881）")
    return True


@pytest.fixture(autouse=True)
def _isolate_registry():
    """每个用例前后保持注册表快照一致。

    不用 SkillRegistry.reset()：reset 会换新实例，而 discover() 对已缓存模块
    是 no-op，reset 后注册表将永远是空的（本项目实测踩坑）。
    改为：先 discover 保证就绪 → 记录快照 → 用例后恢复快照。
    """
    reg = SkillRegistry.instance()
    reg.discover()
    before = dict(reg.skills)
    yield
    reg.skills.clear()
    reg.skills.update(before)
