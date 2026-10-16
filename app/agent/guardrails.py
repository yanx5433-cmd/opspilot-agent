"""护栏层：五道闸门，每道都可独立单测、独立开关、命中即留痕。

1. check_input        目标级拦截（删库、rm -rf、"绕过审批"这类根本不该进规划器）
2. authorize_tool     写操作最小权限（未授权 → blocked 挂起，而不是报错崩溃）
3. sanitize_args      参数消毒（未知参数剔除、必需参数拦截、类型校验、取值域、shell 元字符）
4. filter_output      输出与轨迹脱敏（Key/口令打码）
5. circuit_breaker    预算与连续失败熔断（由 executor 调用）

安全立场：Agent 永远拿不到 shell。工具是声明式只读接口 + 需审批的写接口，
所以"参数消毒"拦的是逻辑越权（比如把主机名写成 `web-01; rm -rf /`），
不是防注入——因为压根没有可注入的执行面。这一点在 README 里要讲清楚。
"""
from __future__ import annotations

import contextlib
import contextvars
import re
from collections.abc import Iterable
from typing import Any

from ..config import get_settings
from .tools_registry import ToolRegistry, check_type, coerce

# 授权清单按上下文隔离，不改进程全局：否则并发请求 A 授的写权限会泄漏给请求 B
_APPROVALS: contextvars.ContextVar[frozenset[str] | None] = contextvars.ContextVar(
    "opspilot_write_approvals", default=None
)


def current_approvals() -> set[str]:
    ov = _APPROVALS.get()
    if ov is not None:
        return set(ov)
    return {t.strip() for t in get_settings().APPROVED_WRITE_TOOLS.split(",") if t.strip()}


@contextlib.contextmanager
def granted_writes(tools: Iterable[str] | None):
    """with granted_writes(["report_render"]): ... —— 作用域内授予写权限。"""
    if tools is None:
        yield
        return
    token = _APPROVALS.set(frozenset(t for t in tools if t))
    try:
        yield
    finally:
        _APPROVALS.reset(token)

# 路径穿越单独判：日志正文里本来就可能出现 "/var/log/../x"，所以 pattern/query 豁免
_PATH_TRAVERSAL = re.compile(r"\.\.[/\\]")


def check_input(goal: str) -> tuple[bool, str]:
    """闸门 1：目标合规。返回 (是否放行, 原因)。"""
    s = get_settings()
    if not goal or not goal.strip():
        return False, "目标为空，无法规划"
    low = goal.lower()
    for pat in s.BLOCKED_GOAL_PATTERNS:
        if pat.lower() in low:
            return False, f"目标包含高危指令「{pat}」，已拦截（P0：不提供破坏性操作能力）"
    for rx in s.BLOCKED_GOAL_REGEXES:
        if re.search(rx, goal):
            return False, (f"目标以自然语言表达了对{re.search(rx, goal).group(0)!r}类破坏性操作的请求，"
                           "已拦截（P0：本系统不存在破坏性能力，也不会代为绕过审批）")
    if len(goal.strip()) < 4:
        return False, "目标过短（<4 字），无法判断意图"
    if len(goal) > 800:
        return False, f"目标过长（{len(goal)} > 800 字），请拆分为多个任务"
    return True, ""


def authorize_tool(tool_name: str, registry: ToolRegistry) -> tuple[bool, str]:
    """闸门 2：写操作授权。只读工具直接放行；写工具必须命中已授权清单。"""
    s = get_settings()
    got = registry.get(tool_name)
    if got is None:
        return False, f"未知工具：{tool_name}（拒绝执行未注册能力）"
    decl = got[0]
    if not decl.side_effect:
        return True, ""
    if not s.WRITE_TOOLS_NEED_APPROVAL:
        return True, ""
    approved = current_approvals()
    if tool_name in approved:
        return True, ""
    return (
        False,
        f"写操作「{tool_name}」未获授权，已挂起等待人工确认"
        f"（当前授权清单：{', '.join(sorted(approved)) or '空'}）",
    )


def sanitize_args(
    tool_name: str, args: dict[str, Any] | None, registry: ToolRegistry
) -> tuple[dict[str, Any], list[str]]:
    """闸门 3：参数消毒。返回 (干净参数, 问题列表)；问题列表非空即由执行器判 failed。"""
    s = get_settings()
    got = registry.get(tool_name)
    if got is None:
        return {}, [f"未知工具：{tool_name}"]
    decl = got[0]
    args = dict(args or {})
    problems: list[str] = []
    clean: dict[str, Any] = {}

    for key, value in args.items():
        if key not in decl.params:
            problems.append(f"剔除未知参数 {key!r}")
            continue
        if isinstance(value, str):
            # 直接按声明清单匹配，不再维护第二套正则：清单说 < 不安全，代码就得拦 <
            bad = [tok for tok in s.UNSAFE_ARG_CHARS if tok in value]
            if bad:
                problems.append(
                    f"参数 {key!r} 含不安全 token {bad[0]!r}"
                    "（清单来源 Settings.UNSAFE_ARG_CHARS，声明与实现同源）")
                continue
            if _PATH_TRAVERSAL.search(value) and key not in ("pattern", "query"):
                problems.append(f"参数 {key!r} 含路径穿越（../ 或 ..\\），已拒绝")
                continue
        typed = coerce(value, decl.params[key])
        if not check_type(decl.params[key], typed):
            problems.append(
                f"参数 {key!r} 类型错误：期望 {decl.params[key]}，实得 {type(typed).__name__}"
            )
            continue
        if not registry.validate_domain(tool_name, key, typed):
            problems.append(f"参数 {key!r}={typed!r} 超出取值域 {decl.enum_params.get(key)}")
            continue
        clean[key] = typed

    for req in decl.required:
        if req not in clean or clean[req] in (None, "", []):
            problems.append(f"缺少必需参数 {req!r}")
    return clean, problems


def filter_output(text: str) -> str:
    """闸门 4：输出层脱敏。"""
    from .trace import mask_text

    return mask_text(text, get_settings().MASK_PATTERNS)


def circuit_breaker(
    budget_used_tools: int, budget_max_tools: int, consecutive_failures: int
) -> tuple[bool, str]:
    """闸门 5：熔断。预算耗尽或连续失败超阈值 → 立刻停止推进。"""
    s = get_settings()
    if budget_used_tools >= budget_max_tools:
        return True, f"工具调用预算耗尽（{budget_used_tools}/{budget_max_tools}）"
    if consecutive_failures >= s.MAX_CONSECUTIVE_FAILURES:
        return True, f"连续失败 {consecutive_failures} 次，熔断退出（避免无意义重试）"
    return False, ""
