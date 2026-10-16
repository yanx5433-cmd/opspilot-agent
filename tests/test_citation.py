"""引用闸门单测：每句结论都必须挂在真实证据上，否则被删掉。

"幻觉"在这里是一个可数的指标（dropped / unevidenced_ratio / numeric_suspects），
所以测试要同时证明两件事：闸门真的会删无据之句，以及它不会误伤合法结论
（"步骤 3：" 这种序号不是测量值，豁免小节的列表不能被裁空）。
"""
from __future__ import annotations

import pytest

from app.agent import synthesizer
from app.agent.evidence import EvidenceLedger
from app.agent.executor import OpsPilotAgent
from app.agent.models import Evidence
from app.agent.synthesizer import VerifyResult, verify

GOALS = [
    "检查 web-01 的磁盘，还能撑多久",
    "db-01 的服务一直在重启，是什么原因",
    "巡检一下 cache-01，看看健康度",
    "巡检 web-01 并给出报告",
    "查一下 mq-01 的队列堆积报错日志",
    "backup-01 有什么配置风险",
    "按手册说明，OOM 怎么处置",
    "巡检 db-01 并开工单",
]


def _ledger_with(*evs: Evidence) -> EvidenceLedger:
    led = EvidenceLedger()
    for e in evs:
        led.add(e)
    return led


def _ev(eid: str = "ev_01", kind: str = "metric", summary: str = "web-01 disk 92.5%",
        data: dict | None = None) -> Evidence:
    return Evidence(id=eid, kind=kind, source=f"t:{eid}", tool="metrics_query",
                    summary=summary, data=data or {"latest": 92.5, "threshold": 85.0})


class TestVerifyGate:
    def test_invented_sentence_without_citation_is_dropped(self):
        led = _ledger_with(_ev())
        text = "## 关键发现\n- 数据库主机负载非常高 `[ev:ev_01]`\n- 我猜是昨天发布引入的回归问题"
        vr = verify(text, led)
        assert vr.dropped == ["- 我猜是昨天发布引入的回归问题"]
        assert "我猜" not in vr.clean
        assert vr.kept and vr.unevidenced_ratio > 0

    def test_all_cited_text_keeps_everything(self):
        led = _ledger_with(_ev("ev_01"), _ev("ev_02", "log"))
        text = "## 关键发现\n- A `[ev:ev_01]`\n- B `[ev:ev_02]`"
        vr = verify(text, led)
        assert vr.dropped == []
        assert vr.unevidenced_ratio == 0.0
        assert "A" in vr.clean and "B" in vr.clean

    def test_invalid_reference_is_flagged_and_stripped(self):
        led = _ledger_with(_ev())
        vr = verify("- 这条引用了不存在的证据 `[ev:ev_99]`", led)
        assert vr.invalid_refs == ["ev_99"]
        assert "ev_99" not in vr.clean
        assert vr.dropped, "引用被剔除后句子就变成无据之句，必须一起被裁掉"

    def test_mixed_valid_and_invalid_ref_keeps_the_valid_one(self):
        led = _ledger_with(_ev())
        vr = verify("- 一半可信 `[ev:ev_99]` `[ev:ev_01]`", led)
        assert vr.invalid_refs == ["ev_99"]
        assert vr.dropped == []
        assert "[ev:ev_01]" in vr.clean

    def test_headings_and_empty_lines_are_not_claims(self):
        led = _ledger_with(_ev())
        vr = verify("## 关键发现\n\n- 有依据 `[ev:ev_01]`", led)
        assert vr.dropped == []

    @pytest.mark.parametrize("line", [
        "结论：见上", "阻塞：需要人工授权", "未覆盖：没有压测", "预算消耗：{'steps': '4/10'}",
        "数据说明：本结论基于确定性演示数据集",
    ])
    def test_structural_lines_are_exempt(self, line):
        led = _ledger_with(_ev())
        vr = verify(line, led)
        assert vr.dropped == [], line

    def test_ordinal_step_numbers_are_not_treated_as_measurements(self):
        """verify 不许把 "步骤 3：" 里的 3 当成需要溯源的数字。"""
        led = _ledger_with(_ev("ev_01", "doc", "手册步骤", data={"steps": ["先抓现场", "再看曲线"]}))
        vr = verify("- 步骤 3：重启后确认服务就绪 `[ev:ev_01]`", led)
        assert vr.numeric_suspects == []

    def test_ordinal_forms_variants(self):
        led = _ledger_with(_ev("ev_01", "doc", data={"steps": ["a"]}))
        vr = verify("- 第 3：来一条\n- 步骤 12: 另一条 `[ev:ev_01]`", led)
        assert vr.numeric_suspects == []

    def test_untraceable_number_is_flagged(self):
        led = _ledger_with(_ev("ev_01", data={"latest": 92.5}))
        vr = verify("- 磁盘已用到 73.1% `[ev:ev_01]`", led)
        assert vr.numeric_suspects, "证据里没有 73.1，这句话的数字是编的"
        assert "73.1" in vr.numeric_suspects[0]

    def test_number_present_in_evidence_is_not_flagged(self):
        led = _ledger_with(_ev("ev_01", data={"latest": 92.5, "points": 24}))
        vr = verify("- 磁盘 92.5%，样本 24 点 `[ev:ev_01]`", led)
        assert vr.numeric_suspects == []

    def test_percent_and_ratio_forms_are_accepted(self):
        """一个数在证据里可能是 0.925 / 92.5 / "92.5%"，展开等价写法才能避免误杀。"""
        led = _ledger_with(_ev("ev_01", data={"ratio": 0.925, "count_str": "第 7 次"}))
        vr = verify("- 命中比例 92.5%，出现 7 次 `[ev:ev_01]`", led)
        assert vr.numeric_suspects == []

    def test_list_length_counts_as_a_number(self):
        led = _ledger_with(_ev("ev_01", "doc", data={"steps": ["a", "b", "c"]}))
        vr = verify("- 手册共 3 步 `[ev:ev_01]`", led)
        assert vr.numeric_suspects == []

    def test_collect_numbers_expands_equivalent_writings(self):
        bag: set[str] = set()
        synthesizer._collect_numbers({"a": 0.26, "b": "约 14 天"}, bag)
        assert {"0.26", "26", "26.0", "14"} <= bag

    def test_collect_numbers_skips_booleans(self):
        """True 不是"1"，否则布尔字段会给数字闸门开后门。"""
        bag: set[str] = set()
        synthesizer._collect_numbers({"ok": True}, bag)
        assert "True" not in bag


class TestExemptSections:
    def test_blocker_bullet_list_survives(self):
        """阻塞与未覆盖描述的是"没发生什么"，天生无证据可引，裁掉它就等于掩盖失败。"""
        led = _ledger_with(_ev())
        text = ("## 关键发现\n- 有据 `[ev:ev_01]`\n\n## 阻塞与未覆盖\n"
                "- 写操作「report_render」未获授权\n- 上游步骤未成功，跳过")
        vr = verify(text, led)
        assert vr.dropped == []
        for line in ("写操作「report_render」未获授权", "上游步骤未成功，跳过"):
            assert line in vr.clean

    def test_exemption_is_reset_by_the_next_heading(self):
        led = _ledger_with(_ev())
        text = ("## 阻塞与未覆盖\n- 未授权\n## 关键发现\n- 这句没有引用，应该被裁掉")
        vr = verify(text, led)
        assert "- 未授权" in vr.clean
        assert vr.dropped == ["- 这句没有引用，应该被裁掉"]

    @pytest.mark.parametrize("heading", ["## 阻塞与未覆盖", "## 说明", "## 附录 A", "## 未覆盖项"])
    def test_all_exempt_headings(self, heading):
        led = _ledger_with(_ev())
        vr = verify(f"{heading}\n- 一条没有引用的说明", led)
        assert vr.dropped == [], heading

    def test_non_exempt_heading_still_requires_citations(self):
        led = _ledger_with(_ev())
        vr = verify("## 处置建议\n- 建议直接扩容三倍", led)
        assert vr.dropped == ["- 建议直接扩容三倍"]


class TestRuleModeAnswerIsFullyCited:
    @pytest.mark.parametrize("goal", GOALS)
    def test_rule_answer_has_zero_unevidenced_claims(self, goal, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(goal, plan_mode="rule")
        led = _ledger_with(*res.evidence)
        vr = verify(res.answer, led)
        assert vr.dropped == [], f"{goal} -> {vr.dropped}"
        assert vr.unevidenced_ratio == 0.0
        assert vr.invalid_refs == []

    @pytest.mark.parametrize("goal", GOALS)
    def test_numeric_suspects_are_empty_on_seeded_data(self, goal, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(goal, plan_mode="rule")
        vr = verify(res.answer, _ledger_with(*res.evidence))
        assert vr.numeric_suspects == [], f"{goal} -> {vr.numeric_suspects}"

    @pytest.mark.parametrize("goal", GOALS)
    def test_every_line_that_needs_a_citation_has_one(self, goal, registry):
        """逐行复核引用：豁免小节（阻塞/未覆盖/说明/附录）不计入。

        被授权跑写操作的那两个目标，其 blocker 句天生无证据可引，
        所以必须按小节粒度豁免——这既是 verify() 的语义，也是这里的前提。
        """
        res = OpsPilotAgent(registry, record_trace=False).run(
            goal, plan_mode="rule", approve=["report_render", "ticket_create"])
        required = _lines_needing_citation(res.answer)
        assert required, goal
        uncited = [ln for ln in required if not synthesizer.CITATION_RE.search(ln)]
        assert uncited == []


def _lines_needing_citation(text: str) -> list[str]:
    """复刻 verify() 的判定：哪些行属于"必须被引用"的事实句。"""
    out: list[str] = []
    exempt = False
    for line in text.splitlines():
        if synthesizer._HEADING.match(line.strip()):
            exempt = bool(synthesizer._EXEMPT_SECTION.search(line))
        if not line.strip() or exempt or not synthesizer._is_factual(line):
            continue
        out.append(line.strip())
    return out

    def test_citation_count_matches_evidence_bookkeeping(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run("巡检 web-01 看看状态", plan_mode="rule")
        cit_event = [e for e in res.events if e.type == "citation"][-1].payload
        assert cit_event["dropped"] == 0
        assert cit_event["unevidenced_ratio"] == 0.0
        assert set(res.citations) == {
            m for m in res.citations if m in {e.id for e in res.evidence}
        }


class TestRefusalPath:
    """回归用例：拒答句曾被自己的引用闸门删成一个空标题。

    那是个真缺陷，且与 ADR-002 的立场（掩盖失败比丢失措辞严重）直接冲突。
    修法不是豁免整个 `## 结论` 小节——那等于给模型留一条"把幻觉写进结论区"的通道——
    而是给"描述本次运行自身"的句子开一条显式元信息通道（synthesizer.meta_line）。
    """

    def test_refusal_sentence_survives_the_gate(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(
            "探测 http://example.com 通不通", plan_mode="rule")
        assert "拒绝无依据作答" in res.answer

    def test_refusal_path_keeps_blockers_visible(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(
            "探测 http://example.com 通不通", plan_mode="rule")
        assert res.status == "failed"
        assert "## 结论" in res.answer and "## 阻塞项" in res.answer
        assert "example.com" in res.answer      # 被拦下的原因必须让值班的人看见
        assert res.dropped_claims == []         # 不再有句子被闸门静默吞掉

    def test_meta_marker_never_reaches_output(self, registry):
        """元信息哨兵只活在闸门内部，绝不能出现在交付文本里。"""
        res = OpsPilotAgent(registry, record_trace=False).run(
            "探测 http://example.com 通不通", plan_mode="rule")
        assert "\x00" not in res.answer

    def test_no_evidence_template_lists_blockers(self):
        text = synthesizer.rule_answer("目标", "explore", EvidenceLedger(), ["上游未授权"],
                                       _budget(), "rule")
        assert "不给结论" in text and "上游未授权" in text

    def test_empty_ledger_and_empty_text_ratio_is_zero(self):
        vr = verify("", EvidenceLedger())
        assert vr.unevidenced_ratio == 0.0 and vr.kept == []


def _budget():
    from app.agent.models import Budget

    return Budget(10, 14, 6, 2)


class TestAdviceBuilder:
    def test_advice_uses_top_hypothesis_and_runbook(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(
            "db-01 为什么一直重启", plan_mode="rule")
        assert "优先按「进程被 OOM Killer 终止」处置" in res.answer
        assert "rb-oom-kill" in res.answer

    def test_advice_reports_capacity_window_only_when_days_are_known(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(
            "检查 web-01 的磁盘还能撑多久", plan_mode="rule")
        assert "已越过告警线" in res.answer
        assert "约 " not in res.answer, "days_to_threshold=0 时不该给出「约 N 天」"

    def test_config_p1_findings_are_listed(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run(
            "backup-01 有什么配置风险", plan_mode="rule")
        assert "配置整改（P1）" in res.answer
        assert "备份开关" in res.answer

    def test_derivable_deliverables_are_suggested_not_executed(self, registry):
        res = OpsPilotAgent(registry, record_trace=False).run("巡检 web-01 看看状态", plan_mode="rule")
        assert "两者均需人工授权" in res.answer
        assert not [st for st in res.steps if st.tool in ("report_render", "ticket_create")]

    def test_verify_result_ratio_math(self):
        vr = VerifyResult(clean="", kept=["a", "b"], dropped=["c"])
        assert vr.unevidenced_ratio == pytest.approx(1 / 3, abs=0.001)
