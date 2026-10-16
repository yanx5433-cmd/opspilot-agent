"""数据模型：步骤、观察证据、轨迹事件、预算、任务结果。"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

StepStatus = Literal["pending", "running", "done", "blocked", "failed", "skipped"]
TaskStatus = Literal["done", "partial", "blocked", "failed"]
EvidenceKind = Literal["metric", "log", "config", "doc", "probe", "ticket", "report"]


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@dataclass
class Step:
    """计划中的一步：一次工具调用。reason 字段是"为什么做这步"，用于可解释性。"""

    id: str
    tool: str
    args: dict[str, Any]
    reason: str
    depends_on: list[str] = field(default_factory=list)
    status: StepStatus = "pending"
    result: Any = None
    error: str | None = None
    started_ms: int | None = None
    ended_ms: int | None = None
    attempts: int = 0
    evidence_ids: list[str] = field(default_factory=list)
    # 反思阶段补的步骤会标 replan_round；用于评测里区分首轮规划质量
    replan_round: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Step:
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Evidence:
    """一次可引用的观察。答复里的每个论断都必须挂在 evidence 上，否则被裁掉。"""

    id: str
    kind: EvidenceKind
    source: str            # 例如 metrics/host-web-01.json、runbook:disk-full
    tool: str
    summary: str           # 一句话结论（人可读）
    data: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    step_id: str = ""
    ts_ms: int = field(default_factory=now_ms)

    @property
    def positive(self) -> bool:
        """这条观察是否真的查到了东西。检索/聚合类工具 0 命中时视为空证据。"""
        total = self.data.get("total")
        if isinstance(total, int) and total == 0:
            return False
        if self.kind == "log":
            return bool(self.data.get("signatures"))
        if self.kind == "doc":
            return any(self.data.get(k) for k in
                       ("refs", "items", "hypotheses", "steps", "upstream", "downstream"))
        return True

    def to_dict(self) -> dict[str, Any]:
        # asdict() 只看字段，会丢掉 positive 这个 property；面板与评测都要它，所以显式序列化
        out = asdict(self)
        out["positive"] = self.positive
        return out


@dataclass
class TraceEvent:
    """轨迹事件：plan / guard / tool_call / observation / reflect / llm / citation / answer。"""

    seq: int
    ts_ms: int
    type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Budget:
    """预算计数器。执行器每推进一步都必须在三个维度上检查（这是真熔断，不是装饰）。"""

    max_steps: int
    max_tool_calls: int
    max_llm_calls: int
    max_replans: int
    steps_used: int = 0
    tool_calls_used: int = 0
    llm_calls_used: int = 0
    replans_used: int = 0

    def allow_step(self) -> bool:
        return self.steps_used < self.max_steps

    def allow_tool(self) -> bool:
        return self.tool_calls_used < self.max_tool_calls

    def allow_llm(self) -> bool:
        return self.llm_calls_used < self.max_llm_calls

    def allow_replan(self) -> bool:
        return self.replans_used < self.max_replans

    def snapshot(self) -> dict[str, Any]:
        return {
            "steps": f"{self.steps_used}/{self.max_steps}",
            "tool_calls": f"{self.tool_calls_used}/{self.max_tool_calls}",
            "llm_calls": f"{self.llm_calls_used}/{self.max_llm_calls}",
            "replans": f"{self.replans_used}/{self.max_replans}",
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskResult:
    """一次 Agent 运行的完整交付物：结论 + 依据 + 轨迹 + 预算消耗 + 阻塞项。"""

    task_id: str
    goal: str
    status: TaskStatus
    answer: str
    steps: list[Step]
    events: list[TraceEvent]
    budget: Budget
    evidence: list[Evidence] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    dropped_claims: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    mode: str = "rule"            # rule | llm（实际走了哪条引擎，评测要分组统计）

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "answer": self.answer,
            "mode": self.mode,
            "steps": [s.to_dict() for s in self.steps],
            "evidence": [e.to_dict() for e in self.evidence],
            "citations": self.citations,
            "dropped_claims": self.dropped_claims,
            "blockers": self.blockers,
            "budget": self.budget.to_dict(),
            "events": [e.to_dict() for e in self.events],
        }
