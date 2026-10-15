"""Skill 注册与发现（架构 §5.3，协议不变——核心冻结项）。

用法::

    from skills.registry import register_skill

    @register_skill
    class MySkill(SpatialSkill): ...

启动时 SkillRegistry.instance().discover() 扫描 backend.skills 全部子模块，
import 即触发装饰器注册。
"""
from __future__ import annotations

import importlib
import pkgutil

from backend.skills.base import SpatialSkill


class SkillRegistry:
    """进程内单例注册表。"""

    _instance: SkillRegistry | None = None
    skills: dict[str, SpatialSkill]

    def __init__(self) -> None:
        self.skills = {}

    @classmethod
    def instance(cls) -> SkillRegistry:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """清空单例（仅限测试）。

        ⚠️ 调用后注册表为**空**：discover() 对已缓存模块是 no-op（见其 docstring），
        不会重新注册。测试隔离请用快照恢复，不要用 reset。
        """
        cls._instance = cls()

    def register(self, skill: SpatialSkill) -> None:
        name = skill.metadata.name
        if name in self.skills:
            raise ValueError(f"Skill 重名: {name}（注册协议要求全局唯一）")
        self.skills[name] = skill

    def get(self, name: str) -> SpatialSkill:
        try:
            return self.skills[name]
        except KeyError as exc:
            raise KeyError(f"Skill 未注册: {name}（已注册 {sorted(self.skills)}）") from exc

    def atomic(self) -> dict[str, SpatialSkill]:
        return {n: s for n, s in self.skills.items() if s.metadata.scene == "atomic"}

    def scenes(self) -> dict[str, SpatialSkill]:
        return {n: s for n, s in self.skills.items() if s.metadata.scene != "atomic"}

    def discover(self, package: str = "backend.skills") -> int:
        """递归扫描 skills 包，import 每个子模块以触发 @register_skill。

        必须递归：原子 Skill 在 backend.skills.atomic.*、场景 Skill 在
        backend.skills.scenes.*（一层 iter_modules 扫不到，实测只发现 2 个）。

        ⚠️ 绝不 reload 模块（本项目实测踩坑）：reload backend.skills.registry
        会**重新定义 SkillRegistry 类**，之后注册进来的 Skill 进入新类的单例，
        而既有代码持有旧类的实例 —— 表现为"Skill 注册了却查不到"。
        因此本方法幂等：已缓存的模块直接跳过，重复调用返回 0。
        测试隔离请用「快照恢复」而非清空单例（见 tests/conftest.py）。
        """
        before = len(self.skills)
        pkg = importlib.import_module(package)
        failures: list[str] = []
        for mod in pkgutil.walk_packages(pkg.__path__, prefix=package + ".", onerror=failures.append):
            try:
                importlib.import_module(mod.name)
            except Exception as exc:  # noqa: BLE001 - 收集后统一报
                failures.append(f"{mod.name}: {exc}")
        if failures:
            raise ImportError("Skill 发现失败:\n  " + "\n  ".join(failures))
        return len(self.skills) - before

    def describe_all(self) -> list[dict]:
        return [s.describe() for s in self.skills.values()]


def register_skill(skill_cls: type[SpatialSkill]) -> type[SpatialSkill]:
    """装饰器：类定义时即注册进全局 Registry（架构 §5.3 原样）。"""
    SkillRegistry.instance().register(skill_cls())
    return skill_cls


__all__ = ["SkillRegistry", "register_skill"]
