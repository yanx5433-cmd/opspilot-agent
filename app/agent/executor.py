"""执行器：规划 → 拓扑执行 → 护栏 → 反思补步 → 引用校验 → 轨迹落盘。

相对"一次性管道"，这里多了四件真正让它成为 Agent 的事：
1. 预算与超时会**真的掐断**（MAX_STEPS / MAX_TOOL_CALLS / MAX_LLM_CALLS / MAX_REPLANS / STEP_TIMEOUT_SEC）；
2. 单步失败只影响依赖它的步骤，其余继续跑（失败传播按 depends_on 判定）；
3. 跑完一轮会反思"证据够不够"，不够就在预算内补只读步骤；
4. 结论必须过引用闸门，无证据的句子会被删掉而不是留下来好看。

工具异常绝不冒泡：无人值守场景里，一个工具挂了应该是"少一条证据"，
而不是"今晚没有巡检报告"。
"""
from __future__ import annotations

import concurrent.futures as _fut
import re
import time
from typing import Any

from ..config import get_settings
from . import guardrails, reflection, synthesizer
from .evidence import EvidenceLedger, harvest
from .models import Budget, Step, TaskResult, TaskStatus, TraceEvent, new_id, now_ms
from .planner import make_plan
from .tools_registry import ToolRegistry
from .trace import TraceWriter, mask_obj

_CITE = re.compile(r"\[ev:([A-Za-z0-9_]+)\]")


def resolve_ref(expr: Any, by_id: dict[str, Step]) -> Any:
    """解析 `$step_id.field.subfield.0` 形式的上游观察引用；解析不出返回 None 由调用方决定。"""
    if not (isinstance(expr, str) and expr.startswith("$") and "." in expr):
        return expr
    parts = expr[1:].split(".")
    step = by_id.get(parts[0])
    if step is None:
        return None
    cur: Any = step.result
    for token in parts[1:]:
        if isinstance(cur, dict):
            cur = cur.get(token)
        elif isinstance(cur, list) and token.isdigit():
            cur = cur[int(token)] if int(token) < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


class _NullWriter:
    """record_trace=False 时用，接口与 TraceWriter 一致。"""

    def __init__(self) -> None:
        self._seq = 0

    def event(self, etype: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        return {"seq": self._seq, "ts_ms": int(time.time() * 1000)}

    def close(self, summary: dict[str, Any]) -> None:
        return None


class OpsPilotAgent:
    def __init__(self, registry: ToolRegistry, trace_dir: str | None = None,
                 record_trace: bool = True) -> None:
        self.registry = registry
        self.settings = get_settings()
        self.trace_dir = trace_dir or self.settings.TRACE_DIR
        self.record_trace = record_trace

    # ------------------------------------------------------------------ 计划合法性
    def _topo_order(self, steps: list[Step]) -> list[Step] | None:
        """Kahn 拓扑排序。返回 None 表示计划不合法（环依赖/悬空依赖）→ fail fast。"""
        by_id = {s.id: s for s in steps}
        indeg = {s.id: 0 for s in steps}
        children: dict[str, list[str]] = {s.id: [] for s in steps}
        for s in steps:
            for dep in s.depends_on:
                if dep not in by_id:
                    return None
                indeg[s.id] += 1
                children[dep].append(s.id)
        ready = [sid for sid, d in indeg.items() if d == 0]
        order: list[Step] = []
        while ready:
            sid = ready.pop(0)
            order.append(by_id[sid])
            for ch in children[sid]:
                indeg[ch] -= 1
                if indeg[ch] == 0:
                    ready.append(ch)
        return order if len(order) == len(steps) else None

    # ------------------------------------------------------------------ 单步
    def _execute_step(
        self, step: Step, by_id: dict[str, Step], ledger: EvidenceLedger,
        budget: Budget, emit: Any,
    ) -> None:
        if not budget.allow_step():
            step.status = "skipped"
            step.error = f"步骤预算耗尽（{budget.steps_used}/{budget.max_steps}）"
            emit("guard", {"gate": "budget", "kind": "steps", "tool": step.tool, "skipped": True})
            return
        if not budget.allow_tool():
            step.status = "skipped"
            step.error = f"工具调用预算耗尽（{budget.tool_calls_used}/{budget.max_tool_calls}）"
            emit("guard", {"gate": "budget", "kind": "tool_calls", "tool": step.tool, "skipped": True})
            return
        budget.steps_used += 1

        bad_deps = [d for d in step.depends_on if by_id[d].status in ("failed", "blocked", "skipped")]
        if bad_deps:
            step.status = "skipped"
            step.error = f"上游步骤未成功（{len(bad_deps)} 个），跳过"
            emit("guard", {"gate": "dependency", "tool": step.tool, "skipped": True, "reason": step.error})
            return

        allowed, why = guardrails.authorize_tool(step.tool, self.registry)
        if not allowed:
            step.status = "blocked"
            step.error = why
            emit("guard", {"gate": "write_auth", "tool": step.tool, "allowed": False, "reason": why})
            return

        resolved: dict[str, Any] = {}
        for key, value in step.args.items():
            if isinstance(value, str) and value.startswith("$evid:"):
                up = by_id.get(value[len("$evid:"):])
                got = list(up.evidence_ids) if up else []
                if not got:
                    step.status = "skipped"
                    step.error = f"上游 {value} 未产生证据，跳过（工单不能没有依据）"
                    emit("guard", {"gate": "reference", "tool": step.tool, "skipped": True, "reason": step.error})
                    return
                resolved[key] = got
            elif isinstance(value, str) and value.startswith("$"):
                got = resolve_ref(value, by_id)
                if got in (None, "", []):
                    step.status = "skipped"
                    step.error = f"上游观察未提供 {value} 所指字段，跳过（不猜参数）"
                    emit("guard", {"gate": "reference", "tool": step.tool, "skipped": True, "reason": step.error})
                    return
                resolved[key] = got
            else:
                resolved[key] = value

        clean, problems = guardrails.sanitize_args(step.tool, resolved, self.registry)
        blocking = [p for p in problems if "缺少必需参数" in p or "类型错误" in p or "超出取值域" in p]
        if blocking:
            step.status = "failed"
            step.error = "；".join(blocking)
            emit("guard", {"gate": "args", "tool": step.tool, "problems": problems})
            return
        if problems:
            emit("guard", {"gate": "args", "tool": step.tool, "problems": problems, "soft": True})

        _, fn = self.registry.get(step.tool)
        step.started_ms = now_ms()
        emit("tool_call", {"step": step.id, "tool": step.tool, "args": clean})
        res, err = self._invoke_with_timeout(fn, clean)
        step.attempts += 1
        budget.tool_calls_used += 1
        if res is None:
            res, err = self._retry_readonly(step, fn, clean, err)
        step.ended_ms = now_ms()

        if isinstance(res, dict) and res.get("ok"):
            step.result = res
            step.status = "done"
            ev = harvest(step, res, ledger)
            emit("observation", {"step": step.id, "tool": step.tool,
                                 "summary": res.get("summary", ""), "evidence": ev.id if ev else None})
        elif isinstance(res, dict) and res.get("ok") is False:
            step.status = "failed"
            step.error = f"{res.get('error', 'tool_returned_not_ok')}：{res.get('summary', '')}"
            emit("observation", {"step": step.id, "tool": step.tool, "error": step.error, "soft": True})
        else:
            step.status = "failed"
            step.error = err or "工具返回结构不合规（应为 dict 且含 ok 字段）"
            emit("observation", {"step": step.id, "tool": step.tool, "error": step.error})

    def _invoke_with_timeout(self, fn: Any, args: dict[str, Any]) -> tuple[Any, str | None]:
        s = self.settings
        pool = _fut.ThreadPoolExecutor(max_workers=1)
        try:
            return pool.submit(fn, **args).result(timeout=s.STEP_TIMEOUT_SEC), None
        except _fut.TimeoutError:
            # 刻意不用 `with ThreadPoolExecutor(...)`：退出 with 会 shutdown(wait=True)，
            # 超时后照样要等工具跑完，"超时"就只约束了返回值、没约束墙钟。
            # 这里 wait=False：本步立即放弃推进；遗留线程无法强杀（Python 限制），
            # 会自然跑完，生产做法应改为子进程 + 硬超时，见 docs/LIMITATIONS.md。
            return None, f"步骤超时（>{s.STEP_TIMEOUT_SEC}s），本步放弃等待"
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _retry_readonly(self, step: Step, fn: Any, clean: dict[str, Any],
                        err: str | None) -> tuple[Any, str | None]:
        """只读工具失败重试一次；写工具绝不自动重试（重复写比漏写更危险）。"""
        decl = self.registry.get(step.tool)
        if decl is None or decl[0].side_effect:
            return None, err
        step.attempts += 1
        res2, err2 = self._invoke_with_timeout(fn, clean)
        return res2, (err2 if res2 is None else None) or err

    # ------------------------------------------------------------------ 主流程
    def run(self, goal: str, approve: list[str] | None = None, plan_mode: str | None = None) -> TaskResult:
        s = self.settings
        task_id = new_id("task")
        events: list[TraceEvent] = []
        budget = Budget(s.MAX_STEPS, s.MAX_TOOL_CALLS, s.MAX_LLM_CALLS, s.MAX_REPLANS)
        ledger = EvidenceLedger()
        writer = (self._new_writer(task_id, goal) if self.record_trace else _NullWriter())

        def emit(etype: str, payload: dict[str, Any]) -> None:
            rec = writer.event(etype, payload)
            events.append(TraceEvent(seq=rec["seq"], ts_ms=rec["ts_ms"], type=etype,
                                     payload=mask_obj(payload, s.MASK_PATTERNS)))

        # 授权只在本次调用内生效（contextvars），不污染进程全局，也不跨请求泄漏
        with guardrails.granted_writes(approve):
            return self._run_inner(goal, task_id, budget, ledger, events, emit, writer, plan_mode)

    def _new_writer(self, task_id: str, goal: str) -> Any:
        return TraceWriter(self.trace_dir, task_id, goal, self.settings.MASK_PATTERNS)

    def _run_inner(self, goal, task_id, budget, ledger, events, emit, writer, plan_mode) -> TaskResult:
        ok, reason = guardrails.check_input(goal)
        if not ok:
            emit("guard", {"gate": "input", "allowed": False, "reason": reason})
            return self._finish(task_id, goal, "blocked", f"任务在规划前被输入护栏拦截：{reason}",
                                [], events, budget, ledger, [], [reason], "rule", writer)
        emit("guard", {"gate": "input", "allowed": True})

        steps, engine, intent, slots, llm_meta = make_plan(goal, self.registry, plan_mode)
        budget.llm_calls_used += int(llm_meta.get("attempts", 0) or 0)
        if engine != "rule":
            emit("llm", {"purpose": "plan", "outcome": engine, "model": llm_meta.get("model"),
                         "detail": {k: v for k, v in llm_meta.items()
                                    if k in {"reason", "outcome", "dropped_unknown_tools", "fallback_reason"}}})
        emit("plan", {"engine": engine, "intent": intent, "slots": slots,
                      "steps": [{"tool": st.tool, "reason": st.reason} for st in steps]})

        order = self._topo_order(steps)
        if order is None:
            reason = "计划存在环依赖或悬空依赖，拒绝执行（fail fast）"
            emit("guard", {"gate": "topology", "allowed": False, "reason": reason})
            return self._finish(task_id, goal, "failed", reason, steps, events, budget, ledger,
                                [], [reason], "rule" if engine == "rule" else "llm", writer)

        mode = "rule" if engine == "rule" else "llm"
        consecutive = 0
        round_no = 0
        pending: list[Step] = order
        tripped = False
        while pending and not tripped:
            by_id = {st.id: st for st in steps}
            for step in pending:
                self._execute_step(step, by_id, ledger, budget, emit)
                consecutive = consecutive + 1 if step.status == "failed" else 0
                trip, why = guardrails.circuit_breaker(
                    budget.tool_calls_used, budget.max_tool_calls, consecutive)
                if trip:
                    emit("guard", {"gate": "circuit_breaker", "allowed": False, "reason": why})
                    tripped = True
                    for rest in steps:
                        if rest.status == "pending":
                            rest.status = "skipped"
                            rest.error = f"熔断：{why}"
                    break
            if tripped:
                break
            pending = []
            verdict, gaps, added = reflection.assess(
                goal, intent, steps, ledger.kinds(positive_only=True), self.registry, slots, budget.replans_used)
            emit("reflect", {"verdict": verdict, "gaps": gaps,
                             "added": [{"tool": a.tool, "reason": a.reason} for a in added]})
            if added and budget.allow_replan():
                budget.replans_used += 1
                round_no += 1
                for a in added:
                    a.replan_round = round_no
                steps.extend(added)
                emit("plan", {"engine": f"replan#{round_no}", "intent": intent, "slots": {},
                              "steps": [{"tool": a.tool, "reason": a.reason} for a in added]})
                pending = self._topo_order(added) or []

        blockers = [st.error for st in steps if st.status in ("blocked", "skipped", "failed") and st.error]
        draft, used_mode = self._compose(goal, intent, ledger, blockers, budget, mode, emit)
        vr = synthesizer.verify(draft, ledger)
        emit("citation", {"kept": len(vr.kept), "dropped": len(vr.dropped),
                          "invalid_refs": vr.invalid_refs[:5],
                          "unevidenced_ratio": vr.unevidenced_ratio,
                          "numeric_suspects": vr.numeric_suspects[:3]})
        answer = vr.clean
        status = self._decide_status(steps, blockers, vr, bool(ledger.items))
        if vr.dropped:
            answer += f"\n\n（注：已裁掉 {len(vr.dropped)} 条无证据支撑的表述，见轨迹 citation 事件。）"
        if vr.numeric_suspects:
            answer += f"\n（注：{len(vr.numeric_suspects)} 处数字未能在被引用证据中溯源，请人工复核。）"
        emit("answer", {"status": status, "mode": used_mode, "chars": len(answer)})
        return self._finish(task_id, goal, status, answer, steps, events, budget, ledger,
                            vr.dropped + vr.invalid_refs, blockers, used_mode, writer)

    def _compose(self, goal, intent, ledger, blockers, budget, mode, emit) -> tuple[str, str]:
        if mode == "llm" and budget.allow_llm():
            text, meta = synthesizer.llm_answer(goal, intent, ledger)
            budget.llm_calls_used += int(meta.get("attempts", 0) or 0)
            emit("llm", {"purpose": "answer", "outcome": meta.get("outcome"),
                         "model": meta.get("model"), "chars": len(text) if text else 0})
            if text:
                return text, "llm"
        return synthesizer.rule_answer(goal, intent, ledger, blockers, budget, mode), mode

    def _decide_status(self, steps, blockers, vr, has_evidence) -> TaskStatus:
        done = [st for st in steps if st.status == "done"]
        if not steps:
            return "failed"
        if not done:
            return "blocked" if any(st.status == "blocked" for st in steps) else "failed"
        degraded = len(done) < len(steps)
        too_many_dropped = vr.unevidenced_ratio > get_settings().MAX_UNEVIDENCED_RATIO
        if degraded or too_many_dropped or vr.numeric_suspects or not has_evidence:
            return "partial"
        return "done"

    def _finish(self, task_id, goal, status, answer, steps, events, budget, ledger,
                dropped, blockers, mode, writer) -> TaskResult:
        res = TaskResult(
            task_id=task_id, goal=goal, status=status, answer=answer, steps=steps,
            events=events, budget=budget, evidence=ledger.items, citations=sorted(set(_CITE.findall(answer))),
            dropped_claims=dropped, blockers=[b for b in blockers if b], mode=mode,
        )
        writer.close({"task_id": task_id, "status": status, "mode": mode, "budget": budget.snapshot(),
                      "counters": {"steps": len(steps), "evidence": len(ledger.items),
                                   "citations": len(res.citations), "dropped": len(dropped)}})
        return res
