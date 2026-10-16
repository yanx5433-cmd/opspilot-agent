"""反思与重规划：跑完一轮后判断"证据够不够回答这个问题"，不够就补步骤。

这是 Agent 与"脚本"的分界线——脚本跑完就完了，Agent 会发现自己缺什么。
但补步骤受三重力约束：预算（MAX_REPLANS / MAX_TOOL_CALLS）、只允许只读工具、
每轮只补最关键的缺口。没有约束的"反思"只是无限自我怀疑。
"""
from __future__ import annotations

import json
from typing import Any

from ..config import get_settings
from .models import Step, new_id
from .tools_registry import ToolRegistry

# 意图 → 交付一个可信结论所必须覆盖的证据种类
REQUIRED_KINDS: dict[str, list[str]] = {
    "inspect_host": ["metric", "config"],
    "diagnose": ["metric", "log", "doc"],
    "capacity": ["metric"],
    "log_triage": ["log"],
    "probe_service": ["probe"],
    "kb_lookup": ["doc"],
    "explore": ["doc"],
}

# 缺口 → 能补上它的工具（按优先级）
GAP_TOOLS: dict[str, list[tuple[str, dict[str, Any], str]]] = {
    "metric": [
        ("metrics_query", {"window_hours": 24, "metric": "cpu"}, "补齐 CPU 水位证据"),
        ("metrics_query", {"window_hours": 24, "metric": "mem"}, "补齐内存水位证据"),
        ("metrics_query", {"window_hours": 24, "metric": "disk"}, "补齐磁盘水位证据"),
    ],
    "log": [
        ("log_search", {"service": "", "pattern": "error|exception|fail|refused", "level": "ERROR", "limit": 5},
         "关键词过窄导致 0 命中，放宽为通用错误模式（含 FATAL）再查一次"),
        ("log_search", {"service": "", "pattern": "", "level": "FATAL", "limit": 5},
         "仍无命中则不带关键词，直接看 FATAL 级别有哪些"),
    ],
    "config": [("config_check", {"component": ""}, "补齐配置体检证据")],
    "probe": [("service_status", {}, "补齐服务在线状态证据")],
    "doc": [("kb_search", {"top_k": 3}, "补齐处置手册证据")],
}


# 必需参数的兜底取值：从已识别槽位里取，取不到就放弃这个候选（见 assess）
SLOT_FALLBACK: dict[str, Any] = {
    "query": lambda sl: str(sl.get("keyword") or "故障 处置"),
    "pattern": lambda sl: str(sl.get("symptom_pattern") or "error"),
    "keyword": lambda sl: str(sl.get("symptom") or sl.get("keyword") or ""),
    "summary": lambda sl: str(sl.get("keyword") or "巡检补查"),
    "message": lambda sl: str(sl.get("keyword") or "巡检补查"),
}


def detect_intent(goal: str) -> str:
    """关键词意图识别。规则优先，LLM 只在规则拿不准时兜底（见 planner）。"""
    low = goal.lower()
    table = [
        ("capacity", ("磁盘", "容量", "写满", "还能撑", "多久", "扩容", "预测")),
        ("probe_service", ("探活", "通不通", "能不能访问", "端口", "http", "健康检查", "服务状态")),
        ("log_triage", ("日志", "报错", "错误", "异常堆栈", "log")),
        ("diagnose", ("为什么", "原因", "根因", "故障", "告警", "挂了", "超时", "排查", "诊断", "OOM")),
        ("inspect_host", ("巡检", "体检", "检查", "看看", "状态", "健康度")),
        ("kb_lookup", ("手册", "runbook", "怎么修", "处置", "方案", "规范", "流程")),
    ]
    for intent, keys in table:
        if any(k in low for k in keys):
            return intent
    return "explore"


def assess(
    goal: str, intent: str, steps: list[Step], evidence_kinds: set[str],
    registry: ToolRegistry, slots: dict[str, Any], replans_used: int,
) -> tuple[str, list[str], list[Step]]:
    """返回 (判定, 缺口列表, 建议补充的步骤)。判定为空表示无需补充。"""
    s = get_settings()
    required = REQUIRED_KINDS.get(intent, ["doc"])
    gaps = [k for k in required if k not in evidence_kinds]
    failed = [st for st in steps if st.status == "failed"]

    if not steps:
        return ("无可执行步骤，终止", gaps, [])
    if len(failed) == len(steps):
        return (f"全部 {len(steps)} 步均失败，终止重规划（继续重试没有意义）", gaps, [])
    if replans_used >= s.MAX_REPLANS and gaps:
        return (f"证据缺口 {gaps} 未补齐，但重规划预算已用完（{replans_used}/{s.MAX_REPLANS}）", gaps, [])
    if not gaps and not failed:
        return (f"证据覆盖达标（{sorted(evidence_kinds)}），无需补充", [], [])

    added: list[Step] = []
    # 去重按"工具 + 参数签名"，而不是按工具名：
    # 同一只工具换更宽的关键词再查一次，正是证据不足时该做的自我纠正。
    used_sigs = {f"{st.tool}|{json.dumps(st.args, sort_keys=True, ensure_ascii=False, default=str)}" for st in steps}
    host = str(slots.get("host") or "")
    for kind in gaps:
        for tool, args, reason in GAP_TOOLS.get(kind, []):
            decl = registry.get(tool)
            if decl is None:
                continue
            if decl[0].side_effect:  # 反思阶段绝不自动引入写操作
                continue
            filled = dict(args)
            if "host" in decl[0].params:
                if not host:
                    break
                filled["host"] = host
            # 补出来的步骤必须真的能执行。必需参数填不上就换下一个候选，
            # 而不是丢出一个注定因「缺少必需参数」失败的步骤——白烧预算，
            # 还在答复里留下一条看起来像系统故障的阻塞项。
            # 只补"候选里根本没写"的必需参数。写成空串是候选作者的有意选择
            # （例如"不带关键词直接看 FATAL"就是 pattern=""），不能拿槽位覆盖掉。
            missing = [r for r in decl[0].required if r not in filled]
            unmet = False
            for req in missing:
                if req in SLOT_FALLBACK:
                    filled[req] = SLOT_FALLBACK[req](slots)
                else:
                    unmet = True
                    break
            if unmet:
                continue
            sig = f"{tool}|{json.dumps(filled, sort_keys=True, ensure_ascii=False, default=str)}"
            if sig in used_sigs:
                continue
            step = Step(id=new_id("step"), tool=tool, args=filled,
                        reason=f"[重规划] {reason}", replan_round=replans_used + 1)
            if added:
                step.depends_on = [added[-1].id]
            added.append(step)
            used_sigs.add(sig)
            break
    verdict = (f"证据缺口 {gaps}，补 {len(added)} 步" if added
               else f"证据缺口 {gaps}，但缺少可用参数（如未识别到主机）无法补齐")
    if failed and not added:
        verdict += f"；另有 {len(failed)} 步失败"
    return (verdict, gaps, added)
