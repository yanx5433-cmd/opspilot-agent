"""证据账本单测：观察 → 可引用对象的全过程。

"结论必须可追溯"在代码里的落点就是这一层：id、种类、来源、可靠度、
以及"0 命中不算覆盖了这一种"这个关键判定（positive）。
"""
from __future__ import annotations

import json

import pytest

from app.agent.evidence import KIND_CONFIDENCE, EvidenceLedger, harvest
from app.agent.models import Evidence, Step
from app.tools import ops


def _step(tool: str = "metrics_query") -> Step:
    return Step(id="st_1", tool=tool, args={}, reason="r")


class TestEvidenceModel:
    def test_positive_only_when_something_was_found(self):
        empty_search = ops.log_search("", "zzz-nothing", "ERROR", 5)
        ev = Evidence(id="ev_01", kind="log", source="logs:search", tool="log_search",
                      summary="未匹配到日志", data={"total": 0, "signatures": []})
        assert ev.positive is False
        assert empty_search["total"] == 0

    def test_zero_total_is_non_positive_for_any_kind(self):
        assert Evidence(id="e", kind="doc", source="s", tool="t", summary="x",
                        data={"total": 0}).positive is False

    def test_log_without_signatures_is_non_positive(self):
        assert Evidence(id="e", kind="log", source="s", tool="t", summary="x",
                        data={"total": 3, "signatures": []}).positive is False

    @pytest.mark.parametrize("payload", [
        {"refs": []}, {"items": []}, {"hypotheses": []}, {"steps": []},
        {"upstream": []}, {"downstream": []}, {},
    ])
    def test_doc_needs_at_least_one_real_content_field(self, payload):
        assert Evidence(id="e", kind="doc", source="s", tool="t", summary="x",
                        data=payload).positive is False

    def test_doc_with_hypotheses_is_positive(self):
        assert Evidence(id="e", kind="doc", source="s", tool="t", summary="x",
                        data={"hypotheses": [{"cause": "OOM"}]}).positive is True

    def test_metric_and_probe_are_positive_by_default(self):
        for kind, data in (("metric", {"latest": 92.5}), ("probe", {"services": []}),
                           ("config", {"findings": []}), ("ticket", {"ticket_id": "T1"}),
                           ("report", {"path": "a.md"})):
            assert Evidence(id="e", kind=kind, source="s", tool="t", summary="x",
                            data=data).positive is True, kind

    def test_to_dict_is_flat_and_serialisable(self):
        ev = Evidence(id="e", kind="metric", source="s", tool="t", summary="x", data={"a": 1})
        assert json.loads(json.dumps(ev.to_dict(), ensure_ascii=False))["id"] == "e"


class TestHarvest:
    def test_ok_result_becomes_evidence(self):
        led = EvidenceLedger()
        res = ops.metrics_query("web-01", 24, "disk")
        ev = harvest(_step(), res, led)
        assert ev is not None and led.items == [ev]
        assert ev.kind == "metric"
        assert ev.source == res["source"]
        assert ev.summary == res["summary"]
        assert ev.confidence == KIND_CONFIDENCE["metric"]
        assert ev.step_id == "st_1"

    def test_control_fields_are_stripped_from_data(self):
        led = EvidenceLedger()
        ev = harvest(_step(), ops.metrics_query("web-01", 24, "disk"), led)
        assert not ({"ok", "summary", "evidence_kind", "source"} & set(ev.data))
        assert ev.data["latest"] > 90

    def test_failed_result_produces_no_evidence(self):
        led = EvidenceLedger()
        assert harvest(_step(), ops.metrics_query("ghost-09", 24, "disk"), led) is None
        assert harvest(_step(), {"ok": False, "summary": "x"}, led) is None
        assert led.items == []

    def test_empty_summary_produces_no_evidence(self):
        """没有 summary 的观察没法被引用，收进账本只会制造"看起来有证据"的空壳。"""
        led = EvidenceLedger()
        assert harvest(_step(), {"ok": True, "summary": "   "}, led) is None
        assert led.items == []

    def test_missing_evidence_kind_defaults_to_doc(self):
        led = EvidenceLedger()
        ev = harvest(_step(), {"ok": True, "summary": "一条观察"}, led)
        assert ev.kind == "doc", "没标种类就按最保守的文档级证据算"
        assert ev.confidence == KIND_CONFIDENCE["doc"] == 0.7

    def test_ids_are_sequential_and_written_back_to_the_step(self):
        led = EvidenceLedger()
        for i in range(3):
            st = _step()
            harvest(st, {"ok": True, "summary": f"观察 {i}"}, led)
            assert st.evidence_ids == [f"ev_{i + 1:02d}"]
        assert led.ids() == ["ev_01", "ev_02", "ev_03"]

    def test_step_reference_is_used_for_source_fallback(self):
        led = EvidenceLedger()
        ev = harvest(_step("my_tool"), {"ok": True, "summary": "x"}, led)
        assert ev.source == "tool:my_tool"


class TestLedger:
    @pytest.fixture
    def ledger(self) -> EvidenceLedger:
        led = EvidenceLedger()
        for eid, kind in [("ev_01", "metric"), ("ev_02", "log"), ("ev_03", "doc")]:
            led.add(Evidence(id=eid, kind=kind, source=f"s:{eid}", tool="t", summary=eid,
                            data={"latest": 1}))
        return led

    def test_get_and_by_kind(self, ledger):
        assert ledger.get("ev_02").kind == "log"
        assert ledger.get("nope") is None
        assert [e.id for e in ledger.by_kind("doc")] == ["ev_03"]
        assert ledger.by_kind("ticket") == []

    def test_kinds_all_vs_positive_only(self):
        led = EvidenceLedger()
        led.add(Evidence(id="e1", kind="log", source="s", tool="t", summary="未匹配到",
                         data={"total": 0, "signatures": []}))
        led.add(Evidence(id="e2", kind="metric", source="s", tool="t", summary="有数",
                         data={"latest": 3}))
        assert led.kinds() == {"log", "metric"}
        assert led.kinds(positive_only=True) == {"metric"}

    def test_weights_follow_confidence_table(self, ledger):
        assert ledger.weights() == {"doc": 0.7, "log": 0.9, "metric": 0.95}

    def test_weights_use_floor_for_unknown_kind(self):
        led = EvidenceLedger()
        led.add(Evidence(id="e", kind="weird", source="s", tool="t", summary="x"))
        assert led.weights() == {"weird": 0.5}

    def test_to_prompt_is_one_json_per_line_and_slim(self, ledger):
        blocks = ledger.to_prompt().splitlines()
        assert len(blocks) == 3
        parsed = json.loads(blocks[0])
        assert set(parsed) == {"ev", "kind", "source", "summary", "data"}

    def test_to_prompt_drops_fields_outside_the_slim_whitelist(self):
        led = EvidenceLedger()
        led.add(Evidence(id="e1", kind="metric", source="s", tool="metrics_query", summary="x",
                         data={"latest": 1.0, "raw_timeseries": [{"ts": i} for i in range(500)]}))
        parsed = json.loads(led.to_prompt())
        assert "latest" in parsed["data"]
        assert "raw_timeseries" not in parsed["data"], "整条时序塞进 prompt 会烧掉上下文预算"

    def test_to_prompt_prefers_high_confidence_when_over_budget(self):
        led = EvidenceLedger()
        led.add(Evidence(id="hi", kind="metric", source="s", tool="t", summary="指标",
                         data={"latest": 1}))
        led.add(Evidence(id="lo", kind="doc", source="s", tool="t", summary="手册",
                         data={"refs": [{"id": "x"}] * 60}))
        text = led.to_prompt(max_chars=200)
        assert "hi" in text
        assert "lo" not in text, "超预算时先丢低可靠度证据"

    def test_to_prompt_handles_unserialisable_values(self):
        led = EvidenceLedger()
        led.add(Evidence(id="e", kind="metric", source="s", tool="t", summary="x",
                         data={"latest": 1, "obj": object()}))
        assert "ev" in led.to_prompt()


class TestConfidenceOrdering:
    def test_realtime_metrics_outrank_documents(self):
        assert KIND_CONFIDENCE["metric"] > KIND_CONFIDENCE["log"] > KIND_CONFIDENCE["doc"]

    def test_write_artifacts_are_certain(self):
        """工单/报告是系统自己产出的事实，可靠度 1.0 合理。"""
        assert KIND_CONFIDENCE["ticket"] == KIND_CONFIDENCE["report"] == 1.0

    def test_every_kind_the_executor_can_emit_is_covered(self):
        from app.agent.models import EvidenceKind

        for kind in EvidenceKind.__args__:
            assert kind in KIND_CONFIDENCE, kind
