"""Agent 运行时核心。对外只暴露 OpsPilotAgent 与 build_registry。"""
from __future__ import annotations

from .executor import OpsPilotAgent, resolve_ref
from .guardrails import authorize_tool, check_input, circuit_breaker, filter_output, sanitize_args
from .llm import LlmClient
from .models import Budget, Evidence, Step, TaskResult, TraceEvent
from .planner import detect_intent, extract_slots, make_plan
from .reflection import assess as reflect_assess
from .synthesizer import VerifyResult, rule_answer, verify
from .tools_registry import ToolDecl, ToolRegistry
from .trace import TRACE_SCHEMA, TraceWriter, read_trace, replay

__all__ = [
    "TRACE_SCHEMA",
    "Budget",
    "Evidence",
    "LlmClient",
    "OpsPilotAgent",
    "Step",
    "TaskResult",
    "ToolDecl",
    "ToolRegistry",
    "TraceEvent",
    "TraceWriter",
    "VerifyResult",
    "authorize_tool",
    "check_input",
    "circuit_breaker",
    "detect_intent",
    "extract_slots",
    "filter_output",
    "make_plan",
    "read_trace",
    "reflect_assess",
    "replay",
    "resolve_ref",
    "rule_answer",
    "sanitize_args",
    "verify",
]
