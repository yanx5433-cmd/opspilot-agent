"""规划器单测：槽位抽取、意图模板、依赖引用，以及 LLM 计划被逐条校验的过程。

规则引擎是评测基线，所以它的输出必须逐字可预期；
LLM 引擎只在 mock 下测（离线、无 Key），重点是"模型给的东西一律先校验再用"。
"""
from __future__ import annotations

import pytest

from app.agent import planner
from app.agent.models import Step
from app.agent.planner import (
    METRIC_ALIASES,
    SYMPTOM_PATTERNS,
    extract_slots,
    llm_plan,
    make_plan,
)


@pytest.fixture
def stub_llm(monkeypatch):
    """把 LlmClient 换成可编程的桩，返回任意 (obj, meta)。"""

    class _Stub:
        payload: object = None
        meta: dict = {"purpose": "plan", "outcome": "ok", "attempts": 1, "model": "stub"}

        def __init__(self) -> None:
            type(self).instances.append(self)

        def available(self) -> bool:
            return True

        def chat_json(self, system, user, purpose="", temperature=0.2):
            type(self).last_user = user
            return self.payload, dict(self.meta)

    instances: list = []
    _Stub.instances = instances
    monkeypatch.setattr(planner, "LlmClient", _Stub)
    return _Stub


class TestExtractSlots:
    def test_host_metric_and_symptom(self):
        slots = extract_slots("检查 web-01 的磁盘，还能撑多久")
        assert slots["host"] == "web-01"
        assert slots["metric"] == "disk"
        assert slots["symptom"] == "磁盘"

    def test_restart_symptom_carries_a_regex(self):
        """第一版真实缺陷：把整句问话当 pattern 必然 0 命中，所以症状词必须翻译成正则。"""
        slots = extract_slots("db-01 的服务一直在重启，是什么原因")
        assert slots["symptom_pattern"] == "restart|reboot|Killed|OOM|killed process"
        assert slots["metric"] == "mem"

    def test_every_symptom_pattern_is_a_valid_regex(self):
        import re

        for label, pattern, metric in SYMPTOM_PATTERNS:
            re.compile(pattern)
            assert isinstance(label, str) and (metric == "mem" or metric == "cpu" or metric == "disk")

    def test_symptom_patterns_prefer_single_pipe(self):
        """正则里的 | 是合法字符，护栏放行靠这一点（见 test_guardrails）。"""
        assert any("|" in p and "||" not in p for _l, p, _m in SYMPTOM_PATTERNS)

    def test_window_hours_from_natural_language(self):
        assert extract_slots("巡检 app-02 近 48 小时的 CPU")["window_hours"] == 48
        assert extract_slots("看看 web-01 最近 3 天的磁盘")["window_hours"] == 72

    def test_no_window_means_default(self):
        assert "window_hours" not in extract_slots("巡检 web-01 的状态")

    def test_url_slot(self):
        assert extract_slots("探测 http://127.0.0.1:8080/health 通不通")["url"] == \
            "http://127.0.0.1:8080/health"

    def test_quoted_keyword_beats_stopword_stripping(self):
        assert extract_slots("「payment.retry」相关的报错日志")["keyword"] == "payment.retry"

    def test_keyword_is_capped(self):
        slots = extract_slots("帮我看看这台机器上那个一直刷新的告警通道到底是怎么回事啊")
        assert len(slots["keyword"]) <= 24

    def test_delivery_intents_only_from_explicit_words(self):
        plain = extract_slots("巡检 web-01 看看状态")
        assert plain["want_report"] is False and plain["want_ticket"] is False
        both = extract_slots("帮我生成 web-02 的巡检报告并开工单")
        assert both["want_report"] is True and both["want_ticket"] is True

    def test_role_fallback_resolves_a_host(self):
        """没写主机名时按角色找：redis → cache-01。"""
        assert extract_slots("redis 的状态如何")["host"] == "cache-01"

    def test_unknown_host_is_not_invented(self):
        assert "host" not in extract_slots("排查 ghost-01 为什么挂了")

    def test_metric_alias_table_is_consistent_with_tool_enums(self, registry):

        declared = set(registry.get("metrics_query")[0].enum_params["metric"])
        assert set(METRIC_ALIASES.values()) <= declared, "别名指向了工具不认识的指标"


class TestTemplates:
    def test_inspect_host_core_steps_in_order(self, registry):
        steps, engine, intent, slots, meta = make_plan("巡检 web-01 看看状态", registry, "rule")
        assert engine == "rule" and intent == "inspect_host"
        assert [st.tool for st in steps] == [
            "metrics_query", "metrics_query", "service_status", "config_check",
            "disk_forecast", "root_cause_rank"]

    def test_root_cause_depends_on_everything_before_it(self, registry):
        steps, *_ = make_plan("巡检 web-01 看看状态", registry, "rule")
        rc = steps[-1]
        assert set(rc.depends_on) == {st.id for st in steps[:-1]}

    def test_report_step_is_derived_not_hardcoded(self, registry):
        steps, *_ = make_plan("巡检 web-01 并给出报告", registry, "rule")
        rep = steps[-1]
        assert rep.tool == "report_render" and rep.depends_on == [steps[-2].id]
        assert rep.args["host"] == "web-01" and rep.args["fmt"] == "markdown"

    def test_ticket_args_come_from_upstream_observations(self, registry):
        steps, *_ = make_plan("巡检 db-01 并开工单", registry, "rule")
        tk = next(st for st in steps if st.tool == "ticket_create")
        rc = steps[-2] if steps[-1] is tk else next(st for st in steps if st.tool == "root_cause_rank")
        assert tk.args["severity"] == f"${rc.id}.severity"
        assert tk.args["evidence_refs"] == f"$evid:{rc.id}"

    def test_capacity_template_forecasts_then_checks_config(self, registry):
        steps, _e, intent, _s, _m = make_plan("web-01 磁盘还能撑多久", registry, "rule")
        assert intent == "capacity"
        assert [st.tool for st in steps] == ["metrics_query", "disk_forecast", "config_check", "kb_search"]
        fc = steps[1]
        assert fc.args == {"host": "web-01", "horizon_days": 14}

    def test_capacity_without_host_degrades_to_kb(self, registry):
        steps, *_ = make_plan("容量规划该怎么做", registry, "rule")
        assert [st.tool for st in steps] == ["kb_search"], "没有主机就不许编一个出来"

    def test_diagnose_chains_runbook_get_from_root_cause(self, registry):
        steps, *_ = make_plan("db-01 为什么一直重启", registry, "rule")
        rb = next(st for st in steps if st.tool == "runbook_get")
        rc = next(st for st in steps if st.tool == "root_cause_rank")
        assert rb.args["runbook_id"] == f"${rc.id}.top_runbook"

    def test_diagnose_without_host_uses_log_as_probe(self, registry):
        steps, *_ = make_plan("服务一直重启是什么原因", registry, "rule")
        assert "metrics_query" not in [st.tool for st in steps]
        rb = next(st for st in steps if st.tool == "runbook_get")
        log = next(st for st in steps if st.tool == "log_search")
        assert rb.args["runbook_id"] == f"${log.id}.top_runbook"

    def test_probe_template(self, registry):
        steps, _e, intent, _s, _m = make_plan(
            "探测 http://127.0.0.1:8080/health 这个服务通不通", registry, "rule")
        assert intent == "probe_service"
        assert steps[0].tool == "http_probe" and steps[0].args["url"] == "http://127.0.0.1:8080/health"

    def test_kb_lookup_template_chains_id_from_search(self, registry):
        steps, *_ = make_plan("OOM 处置手册怎么写", registry, "rule")
        kb, rb = steps
        assert rb.args["runbook_id"] == f"${kb.id}.refs.0.id"

    def test_plan_reason_is_always_present(self, registry):
        for goal in ("巡检 web-01", "查日志", "容量", "探活 http://127.0.0.1/x 通不通", "手册"):
            steps, *_ = make_plan(goal, registry, "rule")
            assert all(st.reason.strip() for st in steps), goal
            assert all(st.id.startswith("step_") for st in steps)

    def test_no_template_emits_a_write_tool_without_explicit_intent(self, registry):
        for goal in ("巡检 web-01 看看状态", "db-01 为什么挂了", "查一下错误日志", "容量规划"):
            steps, *_ = make_plan(goal, registry, "rule")
            writes = [st for st in steps if registry.get(st.tool)[0].side_effect]
            assert writes == [], goal

    def test_writes_only_appear_when_goal_asks_for_deliverables(self, registry):
        steps, *_ = make_plan("巡检 web-01 并给出报告、开工单", registry, "rule")
        writes = [st.tool for st in steps if registry.get(st.tool)[0].side_effect]
        assert writes == ["report_render", "ticket_create"]


class TestMakePlanEngine:
    def test_rule_mode_ignores_llm(self, registry, stub_llm):
        stub_llm.payload = {"steps": [{"tool": "kb_search", "args": {"query": "x", "top_k": 3}}]}
        steps, engine, *_ = make_plan("巡检 web-01", registry, "rule")
        assert engine == "rule"
        assert stub_llm.instances == [], "rule 模式一次都不该碰模型"

    @pytest.mark.parametrize("mode", ["llm", "auto"])
    def test_no_api_key_falls_back_to_rule(self, registry, mode):
        steps, engine, intent, slots, meta = make_plan("巡检 web-01", registry, mode)
        assert engine == "rule" and meta == {}
        assert steps and intent == "inspect_host"

    def test_llm_plan_is_used_when_available(self, registry, stub_llm):
        stub_llm.payload = {"intent": "diagnose", "steps": [
            {"tool": "metrics_query", "args": {"host": "web-01", "window_hours": 24, "metric": "mem"},
             "reason": "先看内存", "depends_on": []},
            {"tool": "root_cause_rank", "args": {"host": "web-01", "lookback_hours": 24},
             "reason": "归因", "depends_on": [0]},
        ]}
        steps, engine, intent, _slots, meta = make_plan("巡检 web-01", registry, "auto")
        assert engine == "llm" and intent == "inspect_host"
        assert meta["intent_from_llm"] == "diagnose"
        assert steps[1].depends_on == [steps[0].id], "下标依赖必须重映射成步骤 id"

    def test_llm_failure_falls_back_and_records_reason(self, registry, stub_llm):
        stub_llm.payload = None
        stub_llm.meta = {"outcome": "failed", "reason": "请求失败或超时", "attempts": 2}
        steps, engine, _i, _s, meta = make_plan("巡检 web-01", registry, "auto")
        assert engine == "llm→rule"
        assert meta["fallback_reason"] == "请求失败或超时"
        assert [st.tool for st in steps] == [
            "metrics_query", "metrics_query", "service_status", "config_check",
            "disk_forecast", "root_cause_rank"]

    def test_llm_prompt_carries_goal_hosts_and_manifest(self, registry, stub_llm):
        stub_llm.payload = {"steps": [{"tool": "kb_search", "args": {"query": "磁盘"}, "reason": "r"}]}
        make_plan("巡检 web-01 的磁盘", registry, "llm")
        user = stub_llm.last_user
        assert "巡检 web-01 的磁盘" in user
        assert "cache-01" in user and "metrics_query" in user


class TestLlmPlanValidation:
    def test_unknown_tools_are_dropped(self, registry, stub_llm):
        stub_llm.payload = {"steps": [
            {"tool": "rm_rf", "args": {}, "reason": "危险"},
            {"tool": "kb_search", "args": {"query": "磁盘"}, "reason": "查手册"},
        ]}
        steps, meta = llm_plan("目标", registry, {})
        assert [st.tool for st in steps] == ["kb_search"]
        assert meta["dropped_unknown_tools"] == ["rm_rf"]

    def test_all_steps_invalid_returns_none(self, registry, stub_llm):
        stub_llm.payload = {"steps": [{"tool": "sudo_rm", "args": {}}]}
        steps, meta = llm_plan("目标", registry, {})
        assert steps is None and meta["outcome"] == "all_steps_invalid"

    @pytest.mark.parametrize("payload", [{"steps": []}, {"steps": "not-a-list"}, {}])
    def test_bad_dict_shapes_return_none(self, registry, stub_llm, payload):
        stub_llm.payload = payload
        steps, meta = llm_plan("目标", registry, {})
        assert steps is None
        assert meta["outcome"] in {"invalid_shape", "all_steps_invalid"}

    @pytest.mark.parametrize("payload", [["not", "a", "dict"], None, "一段自然语言"])
    def test_non_object_payload_returns_none(self, registry, stub_llm, payload):
        """模型回话不是 JSON 对象时不解析、不猜测，直接交回规则引擎。"""
        stub_llm.payload = payload
        steps, _meta = llm_plan("目标", registry, {})
        assert steps is None

    # 该缺陷已修复，原 xfail 已转为正常断言（见 docs/LIMITATIONS.md 的修复记录）
    def test_fallback_reason_is_never_ok(self, registry, stub_llm):
        stub_llm.payload = ["not", "a", "dict"]
        _steps, engine, _i, _s, meta = make_plan("巡检 web-01", registry, "llm")
        assert engine == "llm→rule"
        assert meta["fallback_reason"] != "ok"

    def test_steps_are_truncated_to_max_steps(self, registry, stub_llm, settings):
        settings.MAX_STEPS = 3
        stub_llm.payload = {"steps": [{"tool": "kb_search", "args": {"query": f"q{i}"}, "reason": "r"}
                                      for i in range(10)]}
        steps, _meta = llm_plan("目标", registry, {})
        assert len(steps) == 3

    def test_out_of_range_dependencies_are_ignored(self, registry, stub_llm):
        stub_llm.payload = {"steps": [
            {"tool": "kb_search", "args": {"query": "a"}, "reason": "r", "depends_on": [7, "x", None]},
        ]}
        steps, _meta = llm_plan("目标", registry, {})
        assert steps[0].depends_on == []

    def test_dependency_to_dropped_step_is_ignored(self, registry, stub_llm):
        """被丢弃的步骤不能留下悬空依赖，否则拓扑排序会整盘拒绝。"""
        stub_llm.payload = {"steps": [
            {"tool": "rm_rf", "args": {}, "reason": "危险"},
            {"tool": "kb_search", "args": {"query": "a"}, "reason": "r", "depends_on": [0]},
        ]}
        steps, _meta = llm_plan("目标", registry, {})
        assert steps[0].depends_on == []

    def test_non_dict_args_become_empty(self, registry, stub_llm):
        stub_llm.payload = {"steps": [{"tool": "service_status", "args": "web-01", "reason": "r"}]}
        steps, _meta = llm_plan("目标", registry, {})
        assert steps[0].args == {}

    def test_reason_is_truncated_and_defaulted(self, registry, stub_llm):
        stub_llm.payload = {"steps": [{"tool": "service_status", "args": {"host": "web-01"},
                                      "reason": "很" * 300}]}
        steps, _meta = llm_plan("目标", registry, {})
        assert len(steps[0].reason) == 120
        stub_llm.payload = {"steps": [{"tool": "service_status", "args": {"host": "web-01"}}]}
        steps2, _ = llm_plan("目标", registry, {})
        assert steps2[0].reason == "模型规划"

    def test_llm_steps_still_pass_the_guardrail_gate(self, registry, stub_llm):
        """模型给的参数最终要过闸门 3，所以这里确认它没有把越权参数带到执行器。"""
        from app.agent.guardrails import sanitize_args

        stub_llm.payload = {"steps": [{"tool": "metrics_query",
                                      "args": {"host": "web-01; rm -rf /", "window_hours": 24,
                                               "metric": "disk", "evil": 1}, "reason": "r"}]}
        steps, _meta = llm_plan("目标", registry, {})
        _clean, problems = sanitize_args(steps[0].tool, steps[0].args, registry)
        assert any("不安全 token" in p for p in problems)
        assert any("剔除未知参数" in p for p in problems)


class TestStepModel:
    def test_round_trip_through_dict(self):
        st = Step(id="a", tool="t", args={"x": 1}, reason="r", depends_on=["b"], status="done")
        assert Step.from_dict(st.to_dict()) == st

    def test_from_dict_ignores_unknown_keys(self):
        st = Step.from_dict({"id": "a", "tool": "t", "args": {}, "reason": "r", "junk": 1})
        assert st.id == "a" and not hasattr(st, "junk")
