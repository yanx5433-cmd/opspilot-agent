"""工具注册表：名称 → 实现 + 声明（参数类型、只读/写、证据种类）。

为什么"声明"和"实现"必须同时存在并互相校验：
工具是 Agent 唯一能碰世界的手。LLM 生成的参数是**不可信输入**，
只有先有一份机器可校验的 schema，护栏才能剔除未知参数、拒绝类型错误。
注册时即用 inspect 比对声明与函数签名，防止"声明漂移"（改了实现忘了改声明）。
"""
from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

ToolFn = Callable[..., dict[str, Any]]

TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "float": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
}


@dataclass
class ToolDecl:
    name: str
    description: str
    params: dict[str, str]              # 参数名 → 类型名（"str"/"int"/"list"…）
    required: list[str] = field(default_factory=list)
    side_effect: bool = False           # True = 写操作，默认必须人工授权
    evidence_kind: str = ""             # metric/log/config/doc/probe/ticket/report
    enum_params: dict[str, list[Any]] = field(default_factory=dict)  # 取值域约束
    examples: list[dict[str, Any]] = field(default_factory=list)

    def to_prompt_dict(self) -> dict[str, Any]:
        """给 LLM 看的工具说明书（不含实现细节）。"""
        return {
            "name": self.name,
            "description": self.description,
            "params": self.params,
            "required": self.required,
            "side_effect": self.side_effect,
            "enum_params": self.enum_params,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolDecl, ToolFn]] = {}

    def register(self, decl: ToolDecl, fn: ToolFn) -> None:
        if decl.name in self._tools:
            raise ValueError(f"工具重复注册：{decl.name}")
        sig = inspect.signature(fn)
        declared = set(decl.params)
        actual = set(sig.parameters)
        if declared != actual:
            raise ValueError(
                f"{decl.name}: 声明参数 {sorted(declared)} 与函数签名 {sorted(actual)} 不一致"
            )
        for req in decl.required:
            if req not in decl.params:
                raise ValueError(f"{decl.name}: required '{req}' 不在 params 中")
            p = sig.parameters[req]
            if p.default is not inspect.Parameter.empty:
                raise ValueError(f"{decl.name}: 必需参数 {req} 不该有默认值")
        for name in decl.enum_params:
            if name not in decl.params:
                raise ValueError(f"{decl.name}: 取值域约束了未声明参数 {name}")
        self._tools[decl.name] = (decl, fn)

    def tool(self, **decl_kwargs):
        """装饰器写法：@registry.tool(name=..., description=..., params=...)"""

        def deco(fn: ToolFn) -> ToolFn:
            self.register(ToolDecl(**decl_kwargs), fn)
            return fn

        return deco

    def get(self, name: str) -> tuple[ToolDecl, ToolFn] | None:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def decls(self) -> list[ToolDecl]:
        return [self._tools[n][0] for n in self.names()]

    def read_tools(self) -> list[str]:
        return sorted(n for n, (d, _) in self._tools.items() if not d.side_effect)

    def write_tools(self) -> list[str]:
        return sorted(n for n, (d, _) in self._tools.items() if d.side_effect)

    def prompt_manifest(self) -> list[dict[str, Any]]:
        return [self._tools[n][0].to_prompt_dict() for n in self.names()]

    def validate_domain(self, name: str, key: str, value: Any) -> bool:
        decl = self._tools[name][0]
        domain = decl.enum_params.get(key)
        if domain is None:
            return True
        return value in domain


def check_type(type_name: str, value: Any) -> bool:
    base = type_name.replace("[]", "").strip()
    checker = TYPE_CHECKS.get(base)
    return True if checker is None else checker(value)


def coerce(value: Any, type_name: str) -> Any:
    """宽松但有限：只接受可无损转换的类型收窄，其余原样交给 validate 拒绝。"""
    base = type_name.replace("[]", "").strip()
    try:
        if base == "int" and isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        if base == "float" and isinstance(value, str):
            return float(value)
        if base == "bool" and isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        if base == "str" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if base == "list" and isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
    except (TypeError, ValueError):
        return value
    return value


def dedupe(names: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for n in names:
        seen.setdefault(n, None)
    return list(seen)
