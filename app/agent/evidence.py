"""证据账本：把工具观察变成可引用、可定权、可回溯的一等对象。

规则模式的答复靠它拼，LLM 模式的答复靠它约束，评测靠它算引用率。
没有这一层，"结论有据"就只能是一句口号。
"""
from __future__ import annotations

import json
from typing import Any

from .models import Evidence, Step

# 证据可靠性：实时指标 > 日志 > 探测 > 配置 > 文档/历史（历史只给基线，不能证明当下）
KIND_CONFIDENCE: dict[str, float] = {
    "metric": 0.95, "log": 0.90, "probe": 0.85, "config": 0.80, "doc": 0.70,
    "ticket": 1.0, "report": 1.0,
}

# 进入 LLM 上下文时各种类证据保留的字段（防止把整条时序塞进 prompt）
SLIM_FIELDS: dict[str, tuple[str, ...]] = {
    "metric": ("host", "metric", "latest", "peak", "mean", "trend", "threshold", "anomalies",
               "baseline", "points", "forecast", "samples", "window_hours"),
    "log": ("total", "signatures", "hosts", "by_level", "window"),
    "probe": ("host", "services", "down", "flapping", "status", "latency_ms", "error", "bytes"),
    "config": ("host", "components", "findings", "p1_count", "rules_applied"),
    "doc": ("refs", "total", "items", "avg_mttr_min", "recurring", "hypotheses", "signals",
            "top_cause", "top_probability", "severity", "steps", "title_meta", "symptom",
            "cause", "upstream", "downstream"),
    "ticket": ("ticket_id", "path", "evidence_count", "dry_run", "channel", "message"),
    "report": ("path", "bytes", "sections"),
}


class EvidenceLedger:
    def __init__(self) -> None:
        self.items: list[Evidence] = []
        self._by_id: dict[str, Evidence] = {}

    def add(self, ev: Evidence) -> str:
        self.items.append(ev)
        self._by_id[ev.id] = ev
        return ev.id

    def get(self, ev_id: str) -> Evidence | None:
        return self._by_id.get(ev_id)

    def ids(self) -> list[str]:
        return [e.id for e in self.items]

    def kinds(self, positive_only: bool = False) -> set[str]:
        """证据种类集合。

        positive_only=True 时只算"真的查到了东西"的证据：检索类工具返回 0 命中
        本身是有价值的观察（答复里要写"没查到"），但它不能算作覆盖了该种类，
        否则反思器永远觉得证据齐了，重规划形同虚设。
        """
        items = [e for e in self.items if (not positive_only or e.positive)]
        return {e.kind for e in items}

    def by_kind(self, kind: str) -> list[Evidence]:
        return [e for e in self.items if e.kind == kind]

    def weights(self) -> dict[str, float]:
        """证据种类 → 可靠性权重，供根因与评测复用。"""
        return {k: KIND_CONFIDENCE.get(k, 0.5) for k in sorted(self.kinds())}

    def to_prompt(self, max_chars: int = 3000) -> str:
        """压缩成给 LLM 的证据清单。超预算时优先丢低可靠度证据，不丢实时指标。"""
        ordered = sorted(self.items, key=lambda e: -e.confidence)
        chunks: list[str] = []
        used = 0
        for e in ordered:
            slim = {k: v for k, v in e.data.items() if k in SLIM_FIELDS.get(e.kind, ())}
            block = json.dumps(
                {"ev": e.id, "kind": e.kind, "source": e.source, "summary": e.summary, "data": slim},
                ensure_ascii=False, default=str)
            if used + len(block) > max_chars:
                continue
            chunks.append(block)
            used += len(block)
        return "\n".join(chunks)


def harvest(step: Step, res: dict[str, Any], ledger: EvidenceLedger) -> Evidence | None:
    """把一次工具返回值收进账本；失败或无 summary 的观察不产生证据。"""
    if not isinstance(res, dict) or not res.get("ok", False):
        return None
    summary = str(res.get("summary", "")).strip()
    if not summary:
        return None
    kind = str(res.get("evidence_kind") or "doc")
    data = {k: v for k, v in res.items() if k not in {"ok", "summary", "evidence_kind", "source"}}
    ev = Evidence(
        id=f"ev_{len(ledger.items) + 1:02d}",
        kind=kind,
        source=str(res.get("source") or f"tool:{step.tool}"),
        tool=step.tool,
        summary=summary,
        data=data,
        confidence=KIND_CONFIDENCE.get(kind, 0.5),
        step_id=step.id,
    )
    ledger.add(ev)
    step.evidence_ids = [ev.id]
    return ev
