"""轨迹层：opspilot.trace.v1（JSONL，append-only，可回放、可审计、可评测）。

为什么用 JSONL 而不是单个 JSON 文档：
- Agent 是长跑任务，事件必须**边产生边落盘**，进程崩了也留有部分轨迹；
- 每行一个事件，grep / jq / pandas 都能直接读，接观测系统零成本；
- 首行 header、末行 summary，读回时按 seq 校验连续性即可发现丢行。

脱敏在落盘前完成（mask），敏感串绝不进磁盘。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, TextIO

TRACE_SCHEMA = "opspilot.trace.v1"

_compiled: dict[tuple[str, ...], list[re.Pattern[str]]] = {}


def compile_masks(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    if patterns not in _compiled:
        _compiled[patterns] = [re.compile(p) for p in patterns]
    return _compiled[patterns]


def mask_text(text: str, patterns: tuple[str, ...]) -> str:
    """把密钥/口令类子串打码，保留前 6 字符便于识别"这里曾经有个 Key"。"""
    out = text
    for pat in compile_masks(patterns):
        out = pat.sub(lambda m: (m.group(0)[:6] + "***MASKED***"), out)
    return out


def mask_obj(obj: Any, patterns: tuple[str, ...]) -> Any:
    """递归脱敏：dict/list/str 都要过，防止密钥藏在 result 字段里落盘。"""
    if isinstance(obj, str):
        return mask_text(obj, patterns)
    if isinstance(obj, dict):
        return {k: mask_obj(v, patterns) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_obj(v, patterns) for v in obj]
    return obj


class TraceWriter:
    """流式轨迹写入器。打开即写 header，每个事件一行，close 时补 summary。"""

    def __init__(
        self, trace_dir: str, task_id: str, goal: str, masks: tuple[str, ...]
    ) -> None:
        Path(trace_dir).mkdir(parents=True, exist_ok=True)
        self.path = os.path.join(trace_dir, f"{task_id}.jsonl")
        self.masks = masks
        self.seq = 0
        self._fh: TextIO = open(self.path, "w", encoding="utf-8", newline="\n")
        self._write(
            {
                "schema": TRACE_SCHEMA,
                "kind": "header",
                "task_id": task_id,
                "goal": mask_text(goal, masks),
                "ts_ms": int(__import__("time").time() * 1000),
            }
        )

    def _write(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def event(self, etype: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.seq += 1
        record = {
            "schema": TRACE_SCHEMA,
            "kind": "event",
            "seq": self.seq,
            "ts_ms": int(__import__("time").time() * 1000),
            "type": etype,
            "payload": mask_obj(payload, self.masks),
        }
        self._write(record)
        return record

    def close(self, summary: dict[str, Any]) -> None:
        self.seq += 1
        self._write(
            {
                "schema": TRACE_SCHEMA,
                "kind": "summary",
                "seq": self.seq,
                "task_id": summary.get("task_id"),
                "status": summary.get("status"),
                "mode": summary.get("mode"),
                "budget": summary.get("budget"),
                "counters": summary.get("counters"),
            }
        )
        self._fh.close()


def read_trace(path: str) -> dict[str, Any]:
    """读回轨迹：header + events + summary。seq 不连续即报错（审计可信度的底线）。"""
    header: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("schema") != TRACE_SCHEMA:
                raise ValueError(f"{path}:{lineno} schema 不匹配: {rec.get('schema')!r}")
            kind = rec.get("kind")
            if kind == "header":
                header = rec
            elif kind == "event":
                events.append(rec)
            elif kind == "summary":
                summary = rec
    seqs = [e["seq"] for e in events]
    # header 不占序号：事件从 1 连续递增，summary 序号 = 事件数 + 1
    if seqs != list(range(1, len(seqs) + 1)):
        gaps = sorted(set(range(1, len(seqs) + 1)) - set(seqs)) or ["重复/乱序"]
        raise ValueError(f"轨迹 seq 不连续，缺失序号: {gaps}（文件 {path}）")
    if summary and summary.get("seq") not in (None, len(seqs) + 1):
        raise ValueError(
            f"summary 序号异常：期望 {len(seqs) + 1}，实得 {summary.get('seq')}（文件 {path}）")
    return {"header": header, "events": events, "summary": summary}


def replay(path: str) -> list[str]:
    """把轨迹渲染成人可读的时间线（CLI --replay 与评测回放共用）。"""
    trace = read_trace(path)
    lines = [f"# 轨迹回放 {trace['header'].get('task_id')}  |  目标：{trace['header'].get('goal')}"]
    for ev in trace["events"]:
        p = ev["payload"]
        t = ev["type"]
        if t == "plan":
            lines.append(
                f"[{ev['seq']:>3}] 规划（{p.get('engine')}）→ "
                + " → ".join(s["tool"] for s in p.get("steps", []))
            )
        elif t == "guard":
            flag = "放行" if p.get("allowed", True) else "拦截"
            lines.append(f"[{ev['seq']:>3}] 护栏 {p.get('gate')}：{flag} {p.get('reason', '')}")
        elif t == "tool_call":
            lines.append(f"[{ev['seq']:>3}] 调用 {p.get('tool')} args={json.dumps(p.get('args'), ensure_ascii=False)[:120]}")
        elif t == "observation":
            if p.get("error"):
                lines.append(f"[{ev['seq']:>3}] ✗ {p.get('tool')} 失败：{p['error']}")
            else:
                lines.append(f"[{ev['seq']:>3}] ✓ {p.get('tool')} → {p.get('summary', '')}")
        elif t == "reflect":
            lines.append(f"[{ev['seq']:>3}] 反思：{p.get('verdict')}；补步骤 {len(p.get('added', []))} 个")
        elif t == "llm":
            lines.append(f"[{ev['seq']:>3}] LLM {p.get('purpose')}：{p.get('outcome')}")
        elif t == "citation":
            lines.append(f"[{ev['seq']:>3}] 引用校验：保留 {p.get('kept')} 句，裁掉 {p.get('dropped')} 句")
        elif t == "answer":
            lines.append(f"[{ev['seq']:>3}] 结论落定（{p.get('status')}）")
    s = trace["summary"]
    lines.append(f"[{s.get('seq')}] 结束：{s.get('status')} 预算 {json.dumps(s.get('budget'), ensure_ascii=False)}")
    return lines
