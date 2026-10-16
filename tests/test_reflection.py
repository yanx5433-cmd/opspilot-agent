"""反思与重规划单测：Agent 会发现自己缺证据，但补步是被三重约束住的。

关键立场：反思**绝不引入写操作**（自动补一个 report_render 等于绕过授权），
并且重规划次数受 MAX_REPLANS 硬约束（没有约束的反思只是无限自我怀疑）。
"""
from __future__ import annotations

import json

import pytest

from app.agent.evidence import EvidenceLedger
from app.agent.executor import OpsPilotAgent
from app.agent.models import Budget, Step
from app.agent.reflection import GAP_TOOLS, REQUIRED_KINDS, assess, detect_intent
from app.tools import ops

ZERO_HIT_GOAL = "「payment.retry」相关的报错日志帮我看下"


def _steps(*items: tuple[str, str, dict]) -> list[Step]:
    return [Step(id=i, tool=t, args=a, reason="r") for i, t, a in items]


class TestDetectIntent:
    @pytest.mark.parametrize(("goal", "intent"), [
        ("web-01 磁盘还能撑多久", "capacity"),
        ("这个服务通不通", "probe_service"),
        ("查一下错误日志", "log_triage"),
        ("db-01 为什么挂了", "diagnose"),
        ("巡检 web-01", "inspect_host"),
        ("OOM 怎么处置", "kb_lookup"),
        ("随便说点什么", "explore"),
    ])
    def test_table(self, goal, intent):
        assert detect_intent(goal) == intent

    def test_priority_is_documented_by_order(self):
        """同一句里既有容量词又有诊断词时，capacity 胜出（表顺序即优先级）。"""
        assert detect_intent("磁盘满了为什么，还能撑多久") == "capacity"

    def test_intent_is_english_key(self):
        assert all(isinstance(detect_intent(g), str) and detect_intent(g).isascii()
                   for g in ("巡检", "查日志", "为什么"))

    def test_every_intent_has_a_template_and_required_kinds(self):
        from app.agent.planner import _TEMPLATES

        intents = set(_TEMPLATES) | {"explore"}
        assert intents <= set(REQUIRED_KINDS), "新增意图必须同时声明证据要求"
        assert set(REQUIRED_KINDS) <= intents


class TestAssessUnit:
    def test_no_gaps_no_added_steps(self):
        steps = _steps(("a", "metrics_query", {}), ("b", "config_check", {}))
        verdict, gaps, added = assess("巡检 web-01", "inspect_host", steps,
                                      {"metric", "config"}, _registry(), {"host": "web-01"}, 0)
        assert gaps == [] and added == [] and "证据覆盖达标" in verdict

    def test_gap_produces_exactly_one_step_per_kind(self):
        steps = _steps(("a", "config_check", {}))
        verdict, gaps, added = assess("巡检 web-01", "inspect_host", steps,
                                      {"config"}, _registry(), {"host": "web-01"}, 0)
        assert gaps == ["metric"]
        assert len(added) == 1 and added[0].tool == "metrics_query"
        assert added[0].reason.startswith("[重规划]")
        assert added[0].replan_round == 1

    def test_replan_budget_is_respected(self, settings):
        settings.MAX_REPLANS = 0
        steps = _steps(("a", "config_check", {}))
        verdict, gaps, added = assess("巡检 web-01", "inspect_host", steps,
                                      {"config"}, _registry(), {"host": "web-01"}, 0)
        assert added == [] and "重规划预算已用完" in verdict and "0/0" in verdict

    def test_empty_plan_stops(self):
        verdict, gaps, added = assess("目标", "explore", [], set(), _registry(), {}, 0)
        assert added == [] and "无可执行步骤" in verdict

    def test_all_steps_failed_stops(self):
        steps = _steps(("a", "metrics_query", {}), ("b", "log_search", {}))
        for st in steps:
            st.status = "failed"
        verdict, _gaps, added = assess("目标", "diagnose", steps, set(), _registry(),
                                       {"host": "web-01"}, 0)
        assert added == [] and "终止重规划" in verdict

    def test_blocked_steps_do_not_trigger_retry(self):
        """blocked（等人工授权）不是失败，反复重试它等于绕着护栏转。"""
        steps = _steps(("a", "report_render", {}))
        steps[0].status = "blocked"
        verdict, _gaps, added = assess("目标", "inspect_host", steps,
                                       {"metric", "config"}, _registry(), {"host": "web-01"}, 0)
        assert added == [] and "证据覆盖达标" in verdict

    def test_without_host_it_refuses_to_guess(self):
        """缺主机时宁可不补，也不能编一个 host 出来。"""
        steps = _steps(("a", "config_check", {}))
        _v, _g, added = assess("巡检", "inspect_host", steps, {"config"}, _registry(), {}, 0)
        assert added == []

    def test_same_tool_wider_keyword_is_allowed(self):
        """去重按"工具+参数签名"，同一只工具换个更宽的关键词再查一次正是自我纠正。"""
        steps = _steps(("a", "log_search", {"pattern": "payment.retry", "level": "ERROR",
                                            "service": "", "limit": 5}))
        _v, gaps, added = assess("查日志", "log_triage", steps, set(), _registry(),
                                 {"host": "web-01"}, 0)
        assert gaps == ["log"]
        assert len(added) == 1 and added[0].tool == "log_search"
        assert added[0].args["pattern"] != "payment.retry"

    def test_identical_signature_is_not_readded(self):
        """候选按顺序试：第一条签名已被用过就试第二条，两条都用过就不再补（不浪费预算）。"""
        steps = _steps(
            ("a", "log_search", {"service": "", "pattern": "error|exception|fail|refused",
                                 "level": "ERROR", "limit": 5}),
            ("b", "log_search", {"service": "", "pattern": "", "level": "FATAL", "limit": 5}),
        )
        verdict, gaps, added = assess("查日志", "log_triage", steps, set(), _registry(), {}, 0)
        assert added == []
        assert "无法补齐" in verdict

    def test_second_candidate_is_tried_when_first_is_used(self):
        """只用过第一条时，必须升级到第二条更宽/更低的候选——这就是"自我纠正"。"""
        steps = _steps(("a", "log_search", {"service": "", "pattern": "error|exception|fail|refused",
                                            "level": "ERROR", "limit": 5}))
        _v, _g, added = assess("查日志", "log_triage", steps, set(), _registry(), {}, 0)
        assert len(added) == 1
        assert added[0].args["level"] == "FATAL"

    @pytest.mark.parametrize("kind", sorted(REQUIRED_KINDS["diagnose"]) + ["probe", "report"])
    def test_reflection_never_adds_a_side_effect_tool(self, kind):
        """核心安全断言：任何证据缺口都不允许被"自动补一个写操作"填上。"""
        r = _registry()
        for tool, args, _reason in GAP_TOOLS.get(kind, []):
            assert r.get(tool)[0].side_effect is False, tool
        steps = _steps(("a", "metrics_query", {}))
        _v, _g, added = assess("目标", "diagnose", steps, {"metric", "log"}, r,
                               {"host": "web-01"}, 0)
        for st in added:
            assert r.get(st.tool)[0].side_effect is False

    def test_all_gap_tools_are_read_only_and_registered(self):
        r = _registry()
        for kind, cands in GAP_TOOLS.items():
            assert cands, kind
            for tool, args, _reason in cands:
                assert r.has(tool), f"{kind} -> {tool}"
                assert r.get(tool)[0].side_effect is False
                assert set(args) <= set(r.get(tool)[0].params)

    def test_added_steps_chain_by_dependency(self):
        """多缺口时补出来的步骤串成链，保证拓扑排序稳定、可解释。"""
        r = _registry()
        steps = _steps(("a", "kb_search", {}))
        _v, gaps, added = assess("目标", "diagnose", steps, set(), r, {"host": "web-01"}, 0)
        assert gaps == ["metric", "log", "doc"]
        assert [st.tool for st in added] == ["metrics_query", "log_search", "kb_search"]
        assert added[0].depends_on == []
        assert added[1].depends_on == [added[0].id]
        assert added[2].depends_on == [added[1].id]

    # 该缺陷已修复，原 xfail 已转为正常断言（见 docs/LIMITATIONS.md 的修复记录）
    def test_doc_gap_candidate_is_executable(self):
        from app.agent.guardrails import sanitize_args

        r = _registry()
        steps = _steps(("a", "metrics_query", {}))
        _v, gaps, added = assess("目标", "diagnose", steps, {"metric"}, r, {"host": "web-01"}, 0)
        assert "doc" in gaps
        kb_step = next(st for st in added if st.tool == "kb_search")
        _clean, problems = sanitize_args("kb_search", kb_step.args, r)
        assert problems == [], f"反思补出来的步骤必须真的能执行：{problems}"


class TestReplanEndToEnd:
    def test_zero_hit_keyword_fires_exactly_one_replan(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(ZERO_HIT_GOAL, plan_mode="rule")
        assert res.budget.replans_used == 1, "0 命中要补，补上之后就不该再来一轮"
        replanned = [st for st in res.steps if st.replan_round > 0]
        assert len(replanned) == 1 and replanned[0].tool == "log_search"
        verdicts = [e.payload["verdict"] for e in res.events if e.type == "reflect"]
        assert any("证据缺口" in v for v in verdicts)
        assert any("证据覆盖达标" in v for v in verdicts)

    def test_first_search_really_was_a_zero_hit(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(ZERO_HIT_GOAL, plan_mode="rule")
        first = [e for e in res.events if e.type == "observation"]
        assert any("未匹配到日志" in (e.payload.get("summary") or "") for e in first)

    def test_replan_closed_the_gap_with_real_hits(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(ZERO_HIT_GOAL, plan_mode="rule")
        log_evs = [e for e in res.evidence if e.kind == "log"]
        assert any(e.positive for e in log_evs), "补出来的一步必须真的查到东西"
        assert "命中" in res.answer

    def test_max_replans_zero_forbids_the_extra_step(self, registry, settings):
        settings.MAX_REPLANS = 0
        res = OpsPilotAgent(registry, record_trace=False).run(ZERO_HIT_GOAL, plan_mode="rule")
        assert res.budget.replans_used == 0
        assert [st for st in res.steps if st.replan_round > 0] == []
        assert any("重规划预算已用完" in e.payload["verdict"] for e in res.events
                   if e.type == "reflect")

    def test_healthy_host_needs_no_replan(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(
            "巡检一下 cache-01 的健康度", plan_mode="rule")
        assert res.budget.replans_used == 0
        assert res.status == "done"

    def test_replanned_step_is_marked_in_trace(self, registry, settings):
        """评测要能区分首轮规划质量与补救质量，所以 replan 事件必须进轨迹。"""
        res = OpsPilotAgent(registry).run(ZERO_HIT_GOAL, plan_mode="rule")
        import os

        from app.agent.trace import read_trace

        trace = read_trace(os.path.join(settings.TRACE_DIR, f"{res.task_id}.jsonl"))
        plans = [e for e in trace["events"] if e["type"] == "plan"]
        assert [p["payload"]["engine"] for p in plans][:1] == ["rule"]
        assert plans[-1]["payload"]["engine"] == "replan#1"
        assert plans[-1]["payload"]["steps"][0]["reason"].startswith("[重规划]")

    def test_negative_control_does_not_invent_a_fault_after_replan(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(
            "巡检一下 cache-01，看看有没有问题", plan_mode="rule")
        rc = next(e for e in res.evidence if e.tool == "root_cause_rank")
        assert rc.data["hypotheses"] == []


def _registry():
    from app.tools import build_registry

    return build_registry()


class TestRequiredKinds:
    @pytest.mark.parametrize("intent", sorted(REQUIRED_KINDS))
    def test_kinds_are_real_evidence_vocabulary(self, intent):
        known = {"metric", "log", "config", "doc", "probe", "ticket", "report"}
        assert set(REQUIRED_KINDS[intent]) <= known, intent

    def test_budget_snapshot_reports_replans(self):
        b = Budget(10, 14, 6, 2)
        assert b.snapshot()["replans"] == "0/2"
        assert b.allow_replan()
        b.replans_used = 2
        assert not b.allow_replan()


class TestAssessPayloadShape:
    def test_verdict_is_always_nonempty(self):
        for kinds in (set(), {"metric"}, {"metric", "log", "doc", "config", "probe"}):
            for intent in sorted(REQUIRED_KINDS):
                steps = _steps(("a", "metrics_query", {}))
                verdict, gaps, added = assess("目标", intent, steps, kinds, _registry(),
                                              {"host": "web-01"}, 0)
                assert verdict, f"{intent}/{kinds}"
                assert isinstance(gaps, list) and isinstance(added, list)

    def test_gap_report_is_json_serialisable(self):
        steps = _steps(("a", "config_check", {}))
        _v, gaps, added = assess("目标", "inspect_host", steps, {"config"}, _registry(),
                                 {"host": "web-01"}, 0)
        assert json.dumps({"gaps": gaps, "added": [st.tool for st in added]})

    def test_unknown_intent_falls_back_to_doc_requirement(self):
        steps = _steps(("a", "metrics_query", {}))
        _v, gaps, _a = assess("目标", "no_such_intent", steps, {"metric"}, _registry(), {}, 0)
        assert gaps == ["doc"]


class TestEvidenceIntegration:
    def test_assess_uses_positive_kinds_only(self, registry):
        """0 命中的检索不能算作"覆盖了 log 种类"，否则重规划形同虚设。"""
        zero = ops.log_search("", "zzz-nothing", "ERROR", 5)
        led = EvidenceLedger()
        step = Step(id="s", tool="log_search", args={}, reason="r")
        from app.agent.evidence import harvest

        ev = harvest(step, zero, led)
        assert ev is not None and ev.positive is False
        assert led.kinds() == {"log"}
        assert led.kinds(positive_only=True) == set()
