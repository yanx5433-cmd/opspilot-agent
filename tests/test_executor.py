"""执行器单测：预算真掐断、失败只传播给依赖者、写操作要授权、引用不许猜、轨迹要落盘。

这一层最容易出现"看起来能跑其实失控"的问题，所以每条预算都单独证伪一次：
把 MAX_TOOL_CALLS 设成 1，就必须看到后面的步骤被跳过；设成超时的工具必须被判失败。
测试只用临时注册表里的假工具，绝不往 app/tools 里塞东西。
"""
from __future__ import annotations

import json
import os
import time

import pytest

from app.agent import guardrails
from app.agent.executor import OpsPilotAgent, resolve_ref
from app.agent.models import Step
from app.agent.tools_registry import ToolDecl, ToolRegistry
from app.tools import ops


def read_lines(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _args_problems(res) -> list[str]:
    """从轨迹的 guard(args) 事件里取完整拒绝理由。"""
    out: list[str] = []
    for ev in res.events:
        if ev.type == "guard" and ev.payload.get("gate") == "args":
            out.extend(ev.payload.get("problems", []))
    return out


@pytest.fixture
def inject_plan(monkeypatch):
    """把规划器替换成固定计划：只考察执行器本身，与 planner 的意图识别解耦。"""

    def _inject(steps: list[Step], intent: str = "explore", slots: dict | None = None,
                engine: str = "rule", meta: dict | None = None):
        import app.agent.executor as ex_mod

        plan = (steps, engine, intent, slots or {}, meta or {})
        monkeypatch.setattr(ex_mod, "make_plan", lambda goal, registry, mode=None: plan)

    return _inject


@pytest.fixture
def fake_registry() -> ToolRegistry:
    """测试专用注册表：只读/写/异常/慢/空 五种假工具。"""

    def ok_read(host: str) -> dict:
        return ops.result(f"{host} 指标正常", "metric", "test:ok", host=host)

    def boom() -> dict:
        raise RuntimeError("工具内部炸了")

    def soft_fail(host: str) -> dict:
        return ops.result(f"主机 {host} 不在巡检清单", "metric", "test:fail", ok=False,
                          error="unknown_host")

    def sleeper(delay: float) -> dict:
        time.sleep(delay)
        return ops.result("睡完了", "metric", "test:sleep")

    def writer(host: str) -> dict:
        return ops.result(f"{host} 交付物已落盘", "report", "test:write")

    r = ToolRegistry()
    r.register(ToolDecl(name="ok_read", description="正常只读", params={"host": "str"},
                       required=["host"], side_effect=False, evidence_kind="metric"), ok_read)
    r.register(ToolDecl(name="boom", description="抛异常", params={}, required=[],
                       side_effect=False, evidence_kind="metric"), boom)
    r.register(ToolDecl(name="soft_fail", description="返回失败", params={"host": "str"},
                       required=["host"], side_effect=False, evidence_kind="metric"), soft_fail)
    r.register(ToolDecl(name="sleeper", description="慢工具", params={"delay": "float"},
                       required=["delay"], side_effect=False, evidence_kind="metric"), sleeper)
    r.register(ToolDecl(name="writer", description="写操作", params={"host": "str"},
                       required=["host"], side_effect=True, evidence_kind="report"), writer)
    return r


class TestBudget:
    def test_max_tool_calls_actually_stops(self, registry, inject_plan, settings):
        settings.MAX_TOOL_CALLS = 1
        inject_plan([
            Step(id="s1", tool="metrics_query", args={"host": "web-01", "window_hours": 24,
                                                     "metric": "disk"}, reason="一"),
            Step(id="s2", tool="metrics_query", args={"host": "web-01", "window_hours": 24,
                                                     "metric": "cpu"}, reason="二"),
            Step(id="s3", tool="service_status", args={"host": "web-01"}, reason="三"),
        ], intent="inspect_host")
        res = OpsPilotAgent(registry).run("预算熔断验证目标", plan_mode="rule")
        assert res.budget.tool_calls_used == 1
        assert res.steps[0].status == "done"
        assert [st.status for st in res.steps[1:]] == ["skipped", "skipped"]
        assert all("预算耗尽" in (st.error or "") for st in res.steps[1:])
        assert res.status == "partial", "有步骤没跑完就不能声称 done"
        assert any(e.payload.get("gate") == "circuit_breaker" for e in res.events)

    def test_max_steps_stops_independently_of_tool_budget(self, registry, inject_plan, settings):
        settings.MAX_STEPS = 1
        settings.MAX_TOOL_CALLS = 14
        inject_plan([
            Step(id="s1", tool="metrics_query", args={"host": "web-01", "window_hours": 24,
                                                     "metric": "disk"}, reason="一"),
            Step(id="s2", tool="service_status", args={"host": "web-01"}, reason="二"),
        ], intent="inspect_host")
        res = OpsPilotAgent(registry).run("步骤预算验证目标", plan_mode="rule")
        assert res.steps[1].status == "skipped"
        assert "步骤预算耗尽" in res.steps[1].error

    def test_consecutive_failures_trip_the_breaker(self, fake_registry, inject_plan, settings):
        settings.MAX_CONSECUTIVE_FAILURES = 2
        inject_plan([
            Step(id="a", tool="soft_fail", args={"host": "ghost-01"}, reason="一"),
            Step(id="b", tool="soft_fail", args={"host": "ghost-02"}, reason="二"),
            Step(id="c", tool="ok_read", args={"host": "web-01"}, reason="三"),
        ])
        res = OpsPilotAgent(fake_registry).run("连续失败熔断目标", plan_mode="rule")
        assert [st.status for st in res.steps] == ["failed", "failed", "skipped"]
        assert "熔断" in (res.steps[2].error or "")

    def test_tool_exception_never_bubbles_out_of_run(self, fake_registry, inject_plan):
        inject_plan([Step(id="a", tool="boom", args={}, reason="炸")])
        res = OpsPilotAgent(fake_registry).run("工具异常不外泄目标")
        assert res.steps[0].status == "failed"
        assert "RuntimeError" in res.steps[0].error
        assert res.status == "failed"

    def test_readonly_tool_is_retried_once(self, inject_plan):
        calls = {"n": 0}

        def flaky(host: str) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("抖了一下")
            return ops.result("第二次成功了", "metric", "test:flaky")

        r = ToolRegistry()
        r.register(ToolDecl(name="flaky", description="抖", params={"host": "str"},
                            required=["host"], side_effect=False, evidence_kind="metric"), flaky)
        inject_plan([Step(id="a", tool="flaky", args={"host": "web-01"}, reason="抖")])
        res = OpsPilotAgent(r).run("只读重试目标")
        assert res.steps[0].status == "done"
        assert res.steps[0].attempts == 2
        assert calls["n"] == 2

    def test_write_tool_is_never_auto_retried(self, inject_plan, settings):
        """重复写比漏写更危险：写工具失败就停，不重试。"""
        calls = {"n": 0}

        def flaky_write(host: str) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("超时了")
            return ops.result("写成功了", "report", "t")

        settings.APPROVED_WRITE_TOOLS = "flaky_write"
        r = ToolRegistry()
        r.register(ToolDecl(name="flaky_write", description="抖写", params={"host": "str"},
                            required=["host"], side_effect=True, evidence_kind="report"), flaky_write)
        inject_plan([Step(id="a", tool="flaky_write", args={"host": "web-01"}, reason="写")])
        res = OpsPilotAgent(r).run("写工具不重试目标")
        assert res.steps[0].status == "failed"
        assert calls["n"] == 1


class TestStepTimeout:
    def test_slow_step_is_marked_timeout(self, fake_registry, inject_plan, settings):
        settings.STEP_TIMEOUT_SEC = 0.05
        inject_plan([Step(id="a", tool="sleeper", args={"delay": 0.6}, reason="慢")])
        res = OpsPilotAgent(fake_registry).run("超时应判失败的目标")
        assert res.steps[0].status == "failed"
        assert "超时" in res.steps[0].error
        assert res.steps[0].attempts == 2, "只读工具超时后还会重试一次，attempt 要如实记录"

    def test_timeout_should_bound_wall_clock(self, fake_registry, inject_plan, settings):
        """回归：曾用 `with ThreadPoolExecutor(...)`，退出时 shutdown(wait=True) 把
        "超时"变成只约束返回值、不约束墙钟——睡 1s 的工具要让整次运行多等约 2s。
        现在显式 shutdown(wait=False, cancel_futures=True)，本步立即放弃。
        遗留线程无法强杀（Python 限制），生产应改子进程硬超时，见 docs/LIMITATIONS.md。
        """
        settings.STEP_TIMEOUT_SEC = 0.05
        inject_plan([Step(id="a", tool="sleeper", args={"delay": 1.0}, reason="慢")])
        t0 = time.monotonic()
        OpsPilotAgent(fake_registry).run("超时应当立刻掐断的目标")
        assert time.monotonic() - t0 < 0.5, "0.05s 的超时不该拖成 2s"


class TestWriteApproval:
    def test_write_step_blocked_without_approval(self, fake_registry, inject_plan):
        inject_plan([Step(id="a", tool="writer", args={"host": "web-01"}, reason="写")])
        res = OpsPilotAgent(fake_registry).run("写操作需授权目标")
        assert res.steps[0].status == "blocked"
        assert "未获授权" in res.steps[0].error
        assert res.status == "blocked"
        assert res.blockers
        guard = [e for e in res.events if e.type == "guard" and e.payload.get("gate") == "write_auth"]
        assert guard and guard[0].payload["allowed"] is False

    def test_write_step_runs_when_approved(self, fake_registry, inject_plan):
        inject_plan([Step(id="a", tool="writer", args={"host": "web-01"}, reason="写")])
        res = OpsPilotAgent(fake_registry).run("写操作已授权目标", approve=["writer"])
        assert res.steps[0].status == "done"
        assert res.status == "done"
        assert res.budget.tool_calls_used == 1

    def test_approval_is_scoped_to_this_run_only(self, fake_registry, inject_plan, settings):
        inject_plan([Step(id="a", tool="writer", args={"host": "web-01"}, reason="写")])
        settings.APPROVED_WRITE_TOOLS = ""
        OpsPilotAgent(fake_registry).run("授权必须回滚的目标", approve=["writer"])
        assert settings.APPROVED_WRITE_TOOLS == "", "临时授权不能泄漏到下一次运行"

    def test_blocked_write_consumes_no_tool_call(self, fake_registry, inject_plan):
        inject_plan([Step(id="a", tool="writer", args={"host": "web-01"}, reason="写")])
        res = OpsPilotAgent(fake_registry).run("未授权不该烧预算的目标")
        assert res.budget.tool_calls_used == 0

    def test_unregistered_capability_is_refused(self, fake_registry, inject_plan):
        inject_plan([Step(id="a", tool="rm_rf", args={"host": "web-01"}, reason="越权")])
        res = OpsPilotAgent(fake_registry).run("未注册能力必须拒绝的目标")
        assert res.steps[0].status == "blocked"
        assert "未知工具" in res.steps[0].error


class TestDependencyAndRefs:
    def test_failure_propagates_only_to_dependents(self, fake_registry, inject_plan):
        inject_plan([
            Step(id="a", tool="soft_fail", args={"host": "ghost"}, reason="炸", ),
            Step(id="b", tool="ok_read", args={"host": "web-01"}, reason="依赖 a", depends_on=["a"]),
            Step(id="c", tool="ok_read", args={"host": "web-02"}, reason="独立"),
        ])
        res = OpsPilotAgent(fake_registry).run("失败传播目标")
        assert res.steps[0].status == "failed"
        assert res.steps[1].status == "skipped" and "上游步骤未成功" in res.steps[1].error
        assert res.steps[2].status == "done", "无关步骤必须继续跑完，不能连坐"

    def test_blocked_upstream_also_skips_dependent(self, fake_registry, inject_plan):
        inject_plan([
            Step(id="a", tool="writer", args={"host": "web-01"}, reason="写"),
            Step(id="b", tool="ok_read", args={"host": "web-01"}, reason="依赖写", depends_on=["a"]),
        ])
        res = OpsPilotAgent(fake_registry).run("上游被挂起的目标")
        assert res.steps[1].status == "skipped"

    def test_evid_ref_feeds_ticket_with_real_evidence_ids(self, registry):
        """$evid:<step> 让工单只能带着真实存在的证据出去。"""
        res = OpsPilotAgent(registry).run("巡检 db-01 并开工单", approve=["ticket_create"],
                                          plan_mode="rule")
        ticket = next(st for st in res.steps if st.tool == "ticket_create")
        assert ticket.status == "done"
        refs = ticket.result["record"]["evidence_refs"]
        assert refs and all(r in [e.id for e in res.evidence] for r in refs)
        assert ticket.result["evidence_count"] == len(refs)

    def test_evid_ref_without_evidence_skips_the_step(self, inject_plan):
        def empty() -> dict:
            return ops.result("", "metric", "t")  # 没有 summary → 不产生证据

        r = ToolRegistry()
        r.register(ToolDecl(name="empty", description="空", params={}, required=[],
                            side_effect=False, evidence_kind="metric"), empty)
        r.register(ToolDecl(name="needs", description="要证据", params={"evidence_refs": "list"},
                            required=["evidence_refs"], side_effect=False, evidence_kind="ticket"),
                   lambda evidence_refs: ops.result("建单", "ticket", "t"))
        inject_plan([
            Step(id="src", tool="empty", args={}, reason="空观察"),
            Step(id="use", tool="needs", args={"evidence_refs": "$evid:src"}, reason="要证据",
                 depends_on=["src"]),
        ])
        res = OpsPilotAgent(r).run("工单不能没有依据的目标")
        assert res.steps[1].status == "skipped"
        assert "未产生证据" in res.steps[1].error

    def test_field_ref_missing_skips_instead_of_guessing(self, registry, inject_plan):
        inject_plan([
            Step(id="rc", tool="root_cause_rank", args={"host": "cache-01", "lookback_hours": 24},
                 reason="健康主机没有 top_runbook"),
            Step(id="rb", tool="runbook_get", args={"runbook_id": "$rc.top_runbook"},
                 reason="取手册", depends_on=["rc"]),
        ])
        res = OpsPilotAgent(registry).run("不猜参数的目标", plan_mode="rule")
        assert res.steps[1].status == "skipped"
        assert "不猜参数" in res.steps[1].error

    @pytest.mark.parametrize(("expr", "expected"), [
        ("$s1.a.b", 7), ("$s1.list.1", "y"), ("$s1.missing", None), ("$s1.list.9", None),
        ("$ghost.a", None), ("plain", "plain"), ("$noslash", "$noslash"), ("$s1.a.b.c", None),
    ])
    def test_resolve_ref(self, expr, expected):
        step = Step(id="s1", tool="t", args={}, reason="r",
                    result={"a": {"b": 7}, "list": ["x", "y"]})
        assert resolve_ref(expr, {"s1": step}) == expected


class TestTopology:
    def test_cycle_is_rejected_before_execution(self, fake_registry, inject_plan):
        inject_plan([
            Step(id="a", tool="ok_read", args={"host": "web-01"}, reason="环", depends_on=["b"]),
            Step(id="b", tool="ok_read", args={"host": "web-02"}, reason="环", depends_on=["a"]),
        ])
        res = OpsPilotAgent(fake_registry).run("环依赖必须拒绝的目标")
        assert res.status == "failed"
        assert "环依赖" in res.answer
        assert res.budget.tool_calls_used == 0, "非法计划一步都不许执行"

    def test_dangling_dependency_is_rejected(self, fake_registry, inject_plan):
        inject_plan([Step(id="a", tool="ok_read", args={"host": "web-01"}, reason="悬空",
                          depends_on=["nope"])])
        res = OpsPilotAgent(fake_registry).run("悬空依赖必须拒绝的目标")
        assert res.status == "failed" and res.budget.tool_calls_used == 0

    def test_steps_run_in_topological_order(self, fake_registry, inject_plan):
        inject_plan([
            Step(id="late", tool="ok_read", args={"host": "web-01"}, reason="后置", depends_on=["early"]),
            Step(id="early", tool="ok_read", args={"host": "web-02"}, reason="前置"),
        ])
        res = OpsPilotAgent(fake_registry).run("拓扑顺序目标")
        # steps 列表保持计划原序，执行顺序看 tool_call 事件
        order = [e.payload["step"] for e in res.events if e.type == "tool_call"]
        assert order == ["early", "late"]
        assert all(st.status == "done" for st in res.steps)

    def test_empty_plan_fails(self, fake_registry, inject_plan):
        inject_plan([])
        res = OpsPilotAgent(fake_registry).run("空计划目标")
        assert res.status == "failed"


class TestInputGate:
    def test_destructive_goal_never_reaches_planner(self, registry, monkeypatch):
        import app.agent.executor as ex_mod

        def explode(*_a, **_kw):
            raise AssertionError("被输入护栏拦下的目标不应该进入规划器")

        monkeypatch.setattr(ex_mod, "make_plan", explode)
        res = OpsPilotAgent(registry, record_trace=False).run("rm -rf / 清空所有服务器")
        assert res.status == "blocked"
        assert "规划前被输入护栏拦截" in res.answer
        assert res.steps == [] and res.budget.tool_calls_used == 0
        assert res.events[0].payload["gate"] == "input"

    def test_gate_fires_for_chinese_destructive_phrase(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run("帮我删库")
        assert res.status == "blocked" and "删库" in res.answer


class TestTrace:
    def test_trace_file_is_written_with_expected_layout(self, registry, settings):
        res = OpsPilotAgent(registry).run("巡检 web-01 看看状态", plan_mode="rule")
        path = os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl")
        assert os.path.isfile(path)
        records = read_lines(path)
        assert records[0]["schema"] == "opspilot.trace.v1" and records[0]["kind"] == "header"
        assert records[0]["goal"] == "巡检 web-01 看看状态"
        assert records[-1]["kind"] == "summary"
        assert records[-1]["status"] == res.status
        assert records[-1]["budget"]["tool_calls"] == res.budget.snapshot()["tool_calls"]
        events = [rec for rec in records if rec["kind"] == "event"]
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1)), "事件序号必须连续"
        assert {e["type"] for e in events} >= {"guard", "plan", "tool_call", "observation",
                                               "reflect", "citation", "answer"}

    def test_in_memory_events_match_the_file(self, registry, settings):
        res = OpsPilotAgent(registry).run("巡检 web-02 看看状态", plan_mode="rule")
        records = read_lines(os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl"))
        disk = [(r["seq"], r["type"]) for r in records if r["kind"] == "event"]
        assert disk == [(e.seq, e.type) for e in res.events]

    def test_record_trace_false_writes_no_file(self, registry, settings):
        res = OpsPilotAgent(registry, record_trace=False).run("不落盘的目标", plan_mode="rule")
        if os.path.isdir(settings.TRACE_DIR):
            assert os.listdir(settings.TRACE_DIR) == []
        assert res.events, "不落盘也要在内存里留痕，CLI --json 还要用"

    def test_read_trace_round_trips_a_real_run(self, registry, settings):
        """落盘 → 读回 → 与内存事件逐条对齐。CLI replay 与观测面板都依赖这条契约。"""
        from app.agent.trace import read_trace

        res = OpsPilotAgent(registry).run("巡检 web-01 看看状态", plan_mode="rule")
        path = os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl")
        trace = read_trace(path)
        assert trace["header"]["task_id"] == res.task_id
        assert [e["type"] for e in trace["events"]] == [e.type for e in res.events]
        assert [e["seq"] for e in trace["events"]] == [e.seq for e in res.events]
        assert trace["summary"]["status"] == res.status

    def test_read_trace_detects_a_missing_line(self, registry, settings):
        """seq 校验的真实价值：中间丢一行必须报错，否则审计可以悄悄被裁剪。"""
        from app.agent.trace import read_trace

        res = OpsPilotAgent(registry).run("巡检 web-01 看看状态", plan_mode="rule")
        path = os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl")
        lines = open(path, encoding="utf-8").read().splitlines()
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines[:3] + lines[4:]) + "\n")  # 删掉第 2 个事件
        with pytest.raises(ValueError, match="seq 不连续"):
            read_trace(path)

    def test_read_trace_detects_a_tampered_summary_seq(self, registry, settings):
        from app.agent.trace import read_trace

        res = OpsPilotAgent(registry).run("巡检 web-01 看看状态", plan_mode="rule")
        path = os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl")
        lines = open(path, encoding="utf-8").read().splitlines()
        tail = json.loads(lines[-1])
        tail["seq"] = 999
        lines[-1] = json.dumps(tail, ensure_ascii=False)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        with pytest.raises(ValueError, match="summary 序号异常"):
            read_trace(path)

    def test_replay_renders_a_human_timeline(self, registry, settings):
        from app.agent.trace import replay

        res = OpsPilotAgent(registry).run("巡检 web-01 并给出报告", plan_mode="rule")
        lines = replay(os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl"))
        assert lines[0].startswith("# 轨迹回放")
        assert any("规划（rule）" in ln for ln in lines)
        assert any("护栏 write_auth：拦截" in ln for ln in lines)
        assert any(ln.startswith("[") and "结束：partial" in ln for ln in lines)

    def test_replay_survives_a_failed_tool(self, fake_registry, inject_plan, settings):
        from app.agent.trace import replay

        inject_plan([Step(id="a", tool="soft_fail", args={"host": "ghost"}, reason="失败步骤")])
        res = OpsPilotAgent(fake_registry).run("回放含失败步骤的目标")
        lines = replay(os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl"))
        assert any("✗ soft_fail 失败" in ln for ln in lines)

    def test_secret_never_reaches_disk(self, inject_plan, settings):
        def leaky() -> dict:
            return ops.result("配置里的密钥是 sk-ABCDEFGHIJKL1234，password=S3cr3tValue",
                              "metric", "test:leak")

        r = ToolRegistry()
        r.register(ToolDecl(name="leaky", description="泄漏", params={}, required=[],
                            side_effect=False, evidence_kind="metric"), leaky)
        inject_plan([Step(id="a", tool="leaky", args={}, reason="泄漏")])
        goal = "检查 sk-ZZZZYYYYXXXX12345678 是否泄漏到磁盘"
        res = OpsPilotAgent(r).run(goal)
        raw = open(os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl"), encoding="utf-8").read()
        for secret in ("sk-ZZZZYYYYXXXX12345678", "sk-ABCDEFGHIJKL1234", "S3cr3tValue"):
            assert secret not in raw, f"{secret} 落盘了"
        assert "***MASKED***" in raw, "脱敏要留痕，不然审计看不出这里有过密钥"

    def test_tool_arguments_are_masked_before_disk(self, registry, settings):
        """参数也可能带密钥（LLM 把上下文里的 Key 抄进 pattern），落盘前同样要打码。"""
        res = OpsPilotAgent(registry).run(
            '查日志，pattern 里带 sk-PRIVATESECRET0000 的关键词', plan_mode="rule")
        raw = open(os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl"), encoding="utf-8").read()
        assert "sk-PRIVATESECRET0000" not in raw

    def test_summary_counters_are_present(self, registry, settings):
        res = OpsPilotAgent(registry).run("巡检 web-01 并给出报告", plan_mode="rule")
        records = read_lines(os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl"))
        counters = records[-1]["counters"]
        assert counters["steps"] == len(res.steps)
        assert counters["evidence"] == len(res.evidence)
        assert counters["citations"] == len(res.citations)


class TestStatusDecision:
    def test_partial_when_a_step_is_blocked(self, registry):
        res = OpsPilotAgent(registry).run("巡检 web-01 并给出报告", plan_mode="rule")
        assert res.status == "partial"
        assert any("report_render" in b for b in res.blockers)
        assert "阻塞与未覆盖" in res.answer

    def test_done_when_everything_succeeds(self, registry):
        res = OpsPilotAgent(registry).run("巡检 web-01 看看状态", plan_mode="rule")
        assert res.status == "done"
        assert res.blockers == []

    def test_answer_refuses_without_evidence(self, fake_registry, inject_plan):
        inject_plan([Step(id="a", tool="soft_fail", args={"host": "ghost"}, reason="注定失败")])
        res = OpsPilotAgent(fake_registry).run("无证据必须拒答的目标")
        assert res.evidence == []
        assert "拒绝无依据作答" in res.answer or res.status == "failed"

    def test_citations_extracted_from_answer(self, registry):
        res = OpsPilotAgent(registry).run("巡检 db-01 看看状态", plan_mode="rule")
        assert res.citations
        assert set(res.citations) <= {e.id for e in res.evidence}

    def test_to_dict_is_json_serialisable(self, registry):
        import dataclasses

        res = OpsPilotAgent(registry).run("巡检 web-01 看看状态", plan_mode="rule")
        dumped = json.dumps(res.to_dict(), ensure_ascii=False, default=str)
        assert json.loads(dumped)["status"] == res.status
        assert dataclasses.is_dataclass(res.steps[0])


class TestGuardrailIntegration:
    def test_bad_args_fail_the_step_not_the_run(self, registry, inject_plan):
        inject_plan([
            Step(id="a", tool="metrics_query",
                 args={"host": "web-01", "window_hours": 24, "metric": "gpu"}, reason="非法指标"),
            Step(id="b", tool="service_status", args={"host": "web-01"}, reason="正常步骤"),
        ], intent="inspect_host")
        res = OpsPilotAgent(registry).run("参数越界目标", plan_mode="rule")
        assert res.steps[0].status == "failed" and "超出取值域" in res.steps[0].error
        assert res.steps[1].status == "done"
        assert any(e.payload.get("gate") == "args" for e in res.events)

    def test_shell_meta_in_args_is_stripped(self, registry, inject_plan):
        inject_plan([Step(id="a", tool="log_search",
                          args={"service": "", "pattern": "rm -rf /; cat /etc/shadow",
                                "level": "ERROR", "limit": 5}, reason="污染参数")],
                    intent="log_triage")
        res = OpsPilotAgent(registry).run("参数污染目标", plan_mode="rule")
        assert res.steps[0].status == "failed"
        problems = _args_problems(res)
        assert any("不安全 token" in p for p in problems), problems
        # 步骤上留下的 error 是派生原因（缺少必需参数），完整拒绝理由在轨迹的 guard 事件里
        assert "缺少必需参数" in res.steps[0].error

    def test_traversal_in_host_is_rejected_end_to_end(self, registry, inject_plan):
        inject_plan([Step(id="a", tool="metrics_query",
                          args={"host": "../../etc/passwd", "window_hours": 24, "metric": "disk"},
                          reason="穿越")])
        res = OpsPilotAgent(registry).run("路径穿越目标", plan_mode="rule")
        assert res.steps[0].status == "failed"
        assert any("路径穿越" in p for p in _args_problems(res))

    def test_readonly_tool_still_usable_after_gate_upgrade(self, registry):
        """护栏收紧不能把正常功能打死：正则里的单个竖线必须照常可用。"""
        args = {"service": "", "pattern": "restart|OOM", "level": "ERROR", "limit": 5}
        clean, problems = guardrails.sanitize_args("log_search", args, registry)
        assert problems == []
        assert registry.get("log_search")[1](**clean)["ok"] is True
