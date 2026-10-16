"""工具层单测：14 只工具逐个打通，并且把每一条错误路径钉死。

约定（app/tools/ops.py 的模块注释）：每个工具都返回
{"ok", "summary", "evidence_kind", "source", **data}。这里既测语义，也测协议。
"""
from __future__ import annotations

import json
import os
import urllib.error

import pytest

from app import data_store
from app.agent import guardrails
from app.tools import kb, ops, write

# ------------------------------------------------------------------ 14 只工具的正常路径
ALL_TOOLS = [
    ("metrics_query", lambda: ops.metrics_query("web-01", 24, "disk")),
    ("log_search", lambda: ops.log_search("mysqld", "connection", "ERROR", 5)),
    ("service_status", lambda: ops.service_status("db-01")),
    ("config_check", lambda: ops.config_check("web-01", "")),
    ("disk_forecast", lambda: ops.disk_forecast("web-01", 14)),
    ("http_probe", lambda: ops.http_probe("http://127.0.0.1:1/health", 300)),
    ("incident_history", lambda: ops.incident_history("db-01", "OOM")),
    ("topology_neighbors", lambda: ops.topology_neighbors("web-01")),
    ("root_cause_rank", lambda: ops.root_cause_rank("db-01", 24)),
    ("kb_search", lambda: kb.kb_search("磁盘 写满 清理", 3)),
    ("runbook_get", lambda: kb.runbook_get("rb-disk-full")),
    ("ticket_create", lambda: write.ticket_create("db-01", "P0", "mysqld 被 OOM 终止", ["ev_01"])),
    ("notify_send", lambda: write.notify_send("console", "db-01 疑似 OOM", True)),
    ("report_render", lambda: write.report_render("web-01", "web-01 巡检报告", "markdown")),
]


class TestToolProtocol:
    @pytest.mark.parametrize("name,call", ALL_TOOLS, ids=[t[0] for t in ALL_TOOLS])
    def test_every_tool_returns_the_declared_protocol(self, name, call, registry):
        res = call()
        assert isinstance(res, dict), name
        for key in ("ok", "summary", "evidence_kind", "source"):
            assert key in res, f"{name} 缺少 {key}"
        assert res["ok"] is True, f"{name} 正常入参却失败：{res['summary']}"
        assert res["summary"].strip(), f"{name} 的 summary 为空 → harvest 不会产生证据"
        decl = registry.get(name)[0]
        assert res["evidence_kind"] == decl.evidence_kind

    @pytest.mark.parametrize("name,call", ALL_TOOLS, ids=[t[0] for t in ALL_TOOLS])
    def test_summary_is_masked_clean(self, name, call):
        """工具输出会直接进轨迹与结论，敏感串必须在落盘前就没了。"""
        res = call()
        assert guardrails.filter_output(res["summary"]) == res["summary"]

    def test_registry_names_cover_exactly_these_fourteen(self, registry):
        assert sorted(name for name, _ in ALL_TOOLS) == registry.names()

    def test_describe_counts_match_seeded_dataset(self, seeded_dataset):
        assert seeded_dataset == {"hosts": 8, "metric_points": 576, "log_lines": 893,
                                  "services": 8, "runbooks": 8, "historical_incidents": 8}
        # services 这一项是"登记了服务的主机数"，不是服务实例数——README 里不能写成 14
        assert len(data_store.services()) == 8
        assert sum(len(v) for v in data_store.services().values()) == 14


class TestMetricsQuery:
    def test_web01_disk_is_the_planted_fault(self):
        r = ops.metrics_query("web-01", 24, "disk")
        assert r["latest"] == pytest.approx(92.47, abs=0.1)
        assert r["threshold"] == 85.0
        assert "已超阈值" in r["summary"]

    def test_window_is_clamped_both_ends(self):
        assert ops.metrics_query("web-01", 1, "disk")["window_hours"] == 3
        assert ops.metrics_query("web-01", 1000, "disk")["window_hours"] == 168
        assert ops.metrics_query("web-01", 1000, "disk")["points"] == 72, "数据只有 72 小时"

    def test_all_six_metrics_are_supported(self):
        for metric in ops.VALID_METRICS:
            assert ops.metrics_query("web-01", 24, metric)["ok"] is True, metric

    def test_healthy_host_reports_no_breach(self):
        r = ops.metrics_query("cache-01", 24, "disk")
        assert "未见越限" in r["summary"]
        assert r["anomalies"] == []

    def test_trend_vocabulary_is_closed(self):
        trends = {ops.metrics_query(h, 24, m)["trend"]
                  for h in ("web-01", "cache-01") for m in ("cpu", "mem", "disk")}
        assert trends <= {"上升", "下降", "平稳"}

    def test_unknown_host(self):
        r = ops.metrics_query("ghost-09", 24, "disk")
        assert r["ok"] is False and r["error"] == "unknown_host"
        assert "不在巡检清单" in r["summary"]
        assert "web-01" in r["summary"], "拒绝原因要给出可用清单，否则调用方无法自救"

    def test_bad_metric(self):
        r = ops.metrics_query("web-01", 24, "gpu")
        assert r["ok"] is False and r["error"] == "bad_metric"
        assert "cpu" in r["summary"]


class TestLogSearch:
    def test_level_is_a_floor_not_an_equals(self):
        """level=ERROR 必须连 FATAL 一起命中，否则 db-01 的 OOM（FATAL）会被漏掉。"""
        r = ops.log_search("", "Out of memory", "ERROR", 5)
        assert r["total"] > 0
        assert r["by_level"].get("FATAL", 0) > 0
        assert "FATAL" in r["summary"]

    def test_regex_alternation_works(self):
        r = ops.log_search("", "restart|Killed", "ERROR", 5)
        assert r["ok"] is True
        assert r["total"] > 0

    def test_single_pipe_survives_the_whole_stack(self, registry):
        """端到端：护栏不拦 + 工具正常执行。README 里"正则可用"这句话靠这条钉住。"""
        args = {"service": "", "pattern": "restart|OOM", "level": "ERROR", "limit": 5}
        clean, problems = guardrails.sanitize_args("log_search", args, registry)
        assert problems == []
        r = ops.log_search(**clean)
        assert r["ok"] is True and r["evidence_kind"] == "log"

    def test_zero_hits_is_ok_but_lists_nothing(self):
        r = ops.log_search("", "zzzz-no-such-thing", "ERROR", 5)
        assert r["ok"] is True
        assert r["total"] == 0 and r["signatures"] == [] and r["hosts"] == []
        assert "未匹配到" in r["summary"]

    def test_bad_level(self):
        r = ops.log_search("", "x", "TRACE", 5)
        assert r["ok"] is False and r["error"] == "bad_level"

    def test_invalid_regex_falls_back_to_literal(self):
        """正则写错不该炸，退化为子串匹配——无人值守场景里一个坏 pattern 不能终止巡检。"""
        r = ops.log_search("", "[unclosed", "ERROR", 5)
        assert r["ok"] is True and r["total"] == 0

    def test_service_filter_narrows_results(self):
        all_hits = ops.log_search("", "connection", "ERROR", 5)["total"]
        narrowed = ops.log_search("mysqld", "connection", "ERROR", 5)["total"]
        assert narrowed < all_hits

    def test_top_is_clamped(self):
        r = ops.log_search("", ".", "", 999)
        assert len(r["signatures"]) <= 10
        assert ops.log_search("", ".", "", 0)["signatures"], "limit=0 也要至少给一条聚合结果"

    def test_aggregation_collapses_repeats_into_one_template(self):
        r = ops.log_search("", ".", "ERROR", 5)
        lead = r["signatures"][0]
        assert lead["count"] > 1
        assert "<N>" in lead["signature"]


class TestServiceStatus:
    def test_down_and_flapping_are_separated(self):
        r = ops.service_status("db-01")
        assert r["down"] == ["mysqld-exporter"]
        assert r["flapping"] == ["mysqld"], "restarts_24h>=3 属频繁重启，不是离线"
        assert "1 个离线" in r["summary"] and "1 个频繁重启" in r["summary"]

    def test_healthy_host(self):
        r = ops.service_status("cache-01")
        assert r["down"] == [] and r["flapping"] == []
        assert "全部在线" in r["summary"]

    def test_unknown_host(self):
        assert ops.service_status("ghost-09")["error"] == "unknown_host"


class TestConfigCheck:
    def test_backup01_has_two_p1_findings(self):
        r = ops.config_check("backup-01", "")
        assert r["p1_count"] == 2
        keys = {f["key"] for f in r["findings"]}
        assert keys == {"cert_expire_days", "backup_enabled"}
        assert r["rules_applied"] == 9

    def test_component_filter(self):
        whole = ops.config_check("web-01", "")
        one = ops.config_check("web-01", "nginx")
        assert one["components"] == ["nginx"]
        assert len(one["findings"]) <= len(whole["findings"])
        assert one["findings"] == whole["findings"], "web-01 的整改项全在 nginx 上，app-order 是干净的"

    def test_debug_log_rule_fires_on_web01(self):
        r = ops.config_check("web-01", "nginx")
        hit = {f["key"] for f in r["findings"]}
        assert {"log_level", "cert_expire_days", "keepalive", "worker_connections"} <= hit

    def test_cert_rule_is_two_sided(self):
        """<=30 天是 P1；>365 天也要提示（纳入自动化）。两头都得真的能触发。

        演示数据里最长的剩余有效期是 300 天，所以第二条只能直接验规则谓词本身。
        """
        assert any(f["severity"] == "P1" and f["key"] == "cert_expire_days"
                   for f in ops.config_check("backup-01", "")["findings"])
        rules = [(sev, pred) for key, pred, sev, _ in ops.LINT_RULES if key == "cert_expire_days"]
        assert len(rules) == 2, "证书规则有两条：短期 P1 + 超长期 P2"
        preds = {sev: pred for sev, pred in rules}
        assert preds["P1"](18) is True and preds["P1"](31) is False
        assert preds["P2"](400) is True and preds["P2"](300) is False

    def test_no_seeded_host_triggers_the_long_horizon_cert_rule(self):
        """诚实记录数据边界：现有演示数据没有 >365 天的证书，这条规则在评测里是"未验证路径"。"""
        for host in data_store.host_ids():
            findings = ops.config_check(host, "")["findings"]
            assert not any(f["key"] == "cert_expire_days" and f["severity"] == "P2" for f in findings)

    def test_healthy_host_has_no_p1(self):
        r = ops.config_check("cache-01", "")
        assert r["p1_count"] == 0
        assert "无 P1 风险" in r["summary"]

    def test_unknown_host_and_component(self):
        assert ops.config_check("ghost-09", "")["error"] == "unknown_host"
        assert ops.config_check("web-01", "nginx2")["error"] == "unknown_component"


class TestDiskForecast:
    def test_web01_is_already_over_the_line(self):
        r = ops.disk_forecast("web-01", 14)
        fc = r["forecast"]
        assert fc["confidence"] == "high" and fc["r2"] > 0.99
        assert fc["days_to_threshold"] == 0.0
        assert fc["slope_per_day"] == pytest.approx(fc["slope_per_step"] * 24, abs=1e-3)
        assert "属于当前问题而非预测" in r["summary"]

    def test_noisy_host_refuses_to_promise_a_date(self):
        r = ops.disk_forecast("cache-01", 14)
        assert r["forecast"]["confidence"] == "low"
        assert r["forecast"]["days_to_threshold"] is None
        assert "不给触顶时间" in r["summary"]
        assert r["ok"] is True, "拒答是一种观察，不是工具失败"

    def test_refuses_below_forecast_min_points(self, settings, monkeypatch):
        """FORECAST_MIN_POINTS 是唯一的外推门票：样本不够就不许给天数。"""
        monkeypatch.setattr(settings, "FORECAST_MIN_POINTS", 500)
        r = ops.disk_forecast("web-01", 14)
        assert r["ok"] is False and r["error"] == "insufficient_samples"
        assert "拒绝预测" in r["summary"]
        assert "forecast" not in r

    def test_threshold_and_window_are_reported(self):
        r = ops.disk_forecast("web-01", 14)
        assert r["threshold"] == 85.0
        assert r["window_hours"] == 24 * 14
        assert r["samples"] == 72

    def test_horizon_is_clamped(self):
        assert ops.disk_forecast("web-01", 900)["window_hours"] == 24 * 30
        assert ops.disk_forecast("web-01", 1)["window_hours"] == 24 * 3

    def test_unknown_host(self):
        assert ops.disk_forecast("ghost-09", 14)["error"] == "unknown_host"


class TestHttpProbe:
    """这里只测拒绝路径与本机回环；绝不放行真实外部请求。"""

    def test_external_public_url_is_rejected_by_ssrf_guard(self, no_network):
        r = ops.http_probe("https://example.com/health", 1000)
        assert r["ok"] is False and r["error"] == "not_allowlisted"
        assert "SSRF" in r["summary"]
        assert no_network is not None, "上面若真去连了 example.com，no_network 夹具会直接报错"

    @pytest.mark.parametrize("url", [
        "https://169.254.169.254/latest/meta-data/",  # 云元数据端点
        "http://internal-vault.corp/token",
        "https://attacker.example/x",
    ])
    def test_metadata_and_non_inventory_targets_are_rejected(self, no_network, url):
        assert ops.http_probe(url, 500)["error"] == "not_allowlisted"

    @pytest.mark.parametrize("url", ["ftp://10.12.3.11/x", "file:///etc/passwd", "//nope", ""])
    def test_non_http_scheme_is_rejected(self, url):
        r = ops.http_probe(url, 500)
        assert r["ok"] is False and r["error"] == "bad_scheme"

    def test_inventory_host_is_allowlisted(self, monkeypatch):
        """清单内主机/IP 属于允许的探测面。用假 urlopen 证明"确实走到了连接这一步"，
        而不是真的去解析 web-01 这个不存在于本机的域名。"""
        seen: dict[str, object] = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            raise urllib.error.URLError("模拟：目标不可达")

        monkeypatch.setattr(ops.urllib.request, "urlopen", fake_urlopen)
        r = ops.http_probe("http://web-01:80/health", 300)
        assert seen["url"] == "http://web-01:80/health", "放行判断错了就等于把内网主机也拦了"
        assert r["error"] in {"unreachable", "conn_refused", "timeout"}

    @pytest.mark.parametrize(("exc", "expected"), [
        (ConnectionRefusedError("[Errno 111] Connection refused"), "conn_refused"),
        (OSError("timed out"), "timeout"),
        (urllib.error.URLError("no route to host"), "unreachable"),
    ])
    def test_network_errors_are_classified(self, monkeypatch, exc, expected):
        def fake_urlopen(_req, timeout=None):
            raise exc

        monkeypatch.setattr(ops.urllib.request, "urlopen", fake_urlopen)
        r = ops.http_probe("http://127.0.0.1:1/health", 300)
        assert r["ok"] is True and r["error"] == expected

    def test_http_error_status_is_reported_as_alive_process(self, monkeypatch):
        """502 要区分"探测失败"和"进程活着但服务端报错"——这是两种不同的处置。"""

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 502, "Bad Gateway", {}, None)

        monkeypatch.setattr(ops.urllib.request, "urlopen", fake_urlopen)
        r = ops.http_probe("http://127.0.0.1:1/health", 300)
        assert r["ok"] is True and r["status"] == 502 and r["error"] == "http_error"
        assert "进程活着但服务端报错" in r["summary"]

    def test_probe_success_reports_status_and_bytes(self, monkeypatch):
        class _Resp:
            status = 200

            def read(self, _n):
                return b"ok"

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        monkeypatch.setattr(ops.urllib.request, "urlopen", lambda *a, **k: _Resp())
        r = ops.http_probe("http://127.0.0.1:1/health", 300)
        assert r["ok"] is True and r["status"] == 200 and r["bytes"] == 2
        assert "error" not in r and "探活成功" in r["summary"]

    def test_localhost_failure_is_classified_not_raised(self):
        """没人监听也必须返回分类结果：网络异常本身就是这个工具的输出。"""
        r = ops.http_probe("http://127.0.0.1:1/health", 300)
        assert r["ok"] is True, "探测失败是可观测事实，不是工具故障"
        assert r["error"] in {"timeout", "conn_refused", "unreachable"}, r["summary"]
        assert "探测失败" in r["summary"]
        assert isinstance(r["latency_ms"], int)

    def test_timeout_is_floored_and_capped(self):
        assert ops.http_probe("http://127.0.0.1:1/x", 1)["latency_ms"] < 5000
        assert ops.http_probe("http://127.0.0.1:1/x", 10 ** 9)["ok"] is True


class TestIncidentHistory:
    def test_oom_recurrence_on_db01(self):
        r = ops.incident_history("db-01", "OOM")
        assert r["total"] == 1
        assert r["avg_mttr_min"] == 41.0
        assert r["recurring"] == ["内存不足被 OOM Killer 终止"]

    def test_wildcard_host_matches_all(self):
        assert ops.incident_history("", "证书")["total"] >= 1

    def test_no_match_is_ok_with_zero_total(self):
        r = ops.incident_history("cache-01", "zzznotexist")
        assert r["ok"] is True and r["total"] == 0 and r["avg_mttr_min"] is None

    def test_required_keyword_is_enforced_by_declaration(self, registry):
        _, problems = guardrails.sanitize_args("incident_history", {"host": "db-01", "keyword": ""}, registry)
        assert any("缺少必需参数 'keyword'" in p for p in problems)


class TestTopology:
    def test_web01_downstream(self):
        r = ops.topology_neighbors("web-01")
        assert r["upstream"] == []
        assert r["downstream"] == ["app-01", "app-02"]
        assert set(r["downstream_detail"]) == {"app-01", "app-02"}

    def test_leaf_host(self):
        r = ops.topology_neighbors("cache-01")
        assert r["downstream"] == []
        assert r["upstream"] == ["app-01"]

    def test_unknown_host(self):
        assert ops.topology_neighbors("ghost-09")["error"] == "unknown_host"


class TestRootCauseRank:
    def test_db01_names_oom_first(self):
        r = ops.root_cause_rank("db-01", 24)
        assert "OOM" in r["hypotheses"][0]["cause"]
        assert r["severity"] == "P0"
        assert r["top_runbook"] == "rb-oom-kill"
        assert "oom" in r["fired_rules"]
        assert r["signals"]["logs"]["oom"] > 0

    def test_web01_names_disk(self):
        r = ops.root_cause_rank("web-01", 24)
        assert r["top_cause"] == "磁盘水位过高（写满前兆）"
        assert "disk_high" in r["fired_rules"]

    def test_negative_control_invents_nothing(self):
        """(c) 的地基：cache-01 全程健康，根因列表必须是空的。"""
        r = ops.root_cause_rank("cache-01", 24)
        assert r["ok"] is True
        assert r["hypotheses"] == []
        assert r["top_cause"] is None
        assert r["fired_rules"] == []
        assert "未发现可归因的异常证据" in r["summary"]

    def test_hypotheses_are_scored_and_sorted(self):
        r = ops.root_cause_rank("db-01", 24)
        scores = [h["score"] for h in r["hypotheses"]]
        assert scores == sorted(scores, reverse=True)
        assert all(0 < s <= 1 for s in scores)
        assert sum(h["probability"] for h in r["hypotheses"]) == pytest.approx(1.0, abs=0.02)

    def test_config_evidence_feeds_the_cert_hypothesis(self):
        r = ops.root_cause_rank("backup-01", 24)
        assert r["top_cause"] == "TLS 证书临近过期"
        assert r["severity"] == "P1"
        assert r["signals"]["config"]["p1_count"] == 2

    def test_unknown_host_and_window_clamp(self):
        assert ops.root_cause_rank("ghost-09", 24)["error"] == "unknown_host"
        assert ops.root_cause_rank("web-01", 1)["host"] == "web-01"


class TestKbTools:
    def test_kb_search_returns_ranked_refs_with_sources(self):
        r = kb.kb_search("磁盘 写满 清理", 3)
        assert r["total"] >= 1
        assert r["refs"][0]["id"] == "rb-disk-full"
        assert r["refs"][0]["source"].startswith("runbooks/")
        assert r["refs"][0]["steps_count"] >= 5
        scores = [x["score"] for x in r["refs"]]
        assert scores == sorted(scores, reverse=True)
    def test_top_k_is_clamped(self):
        assert kb.kb_search("磁盘", 99)["total"] <= 8
        assert kb.kb_search("磁盘", -3)["total"] == 1
        assert kb.kb_search("磁盘", 0)["total"] <= 3, "top_k=0 走 RAG_TOP_K"

    def test_no_match_returns_empty_refs_not_failure(self):
        r = kb.kb_search("qqqzzzwww", 3)
        assert r["ok"] is True and r["total"] == 0 and r["refs"] == []

    def test_terms_counted_for_debuggability(self):
        assert kb.kb_search("磁盘", 3)["terms"] > 0

    def test_runbook_get_full_steps(self):
        r = kb.runbook_get("rb-oom-kill")
        assert r["id"] == "rb-oom-kill"
        assert len(r["steps"]) == 6
        assert r["severity"] == "P0"
        assert "Killed" in r["symptom"]

    def test_runbook_get_unknown_id(self):
        r = kb.runbook_get("rb-not-exist")
        assert r["ok"] is False and r["error"] == "unknown_runbook"
        assert "rb-disk-full" in r["summary"], "要给出可用 id 清单"

    def test_reset_index_rebuilds_after_reseed(self):
        before = kb.kb_search("证书", 3)["refs"][0]["score"]
        kb.reset_index()
        assert kb.kb_search("证书", 3)["refs"][0]["score"] == before

    def test_every_runbook_is_reachable(self):
        for book in data_store.runbooks():
            rid = book.get("id", book["file"])
            assert kb.runbook_get(rid)["ok"] is True, rid


class TestTicketCreate:
    def test_happy_path_writes_auditable_ledger(self, settings):
        r = write.ticket_create("db-01", "P0", "mysqld 被 OOM 终止", ["ev_01", "ev_02"])
        assert r["ok"] is True and r["evidence_count"] == 2
        assert r["path"].startswith(settings.ARTIFACT_DIR)
        assert os.path.isfile(r["path"])
        line = json.loads(open(r["path"], encoding="utf-8").read().strip().splitlines()[-1])
        assert line["evidence_refs"] == ["ev_01", "ev_02"]
        assert line["severity"] == "P0" and line["source"] == "opspilot-agent"

    def test_refuses_without_evidence(self):
        for refs in ([], [""], ["  ", ""], None):
            r = write.ticket_create("db-01", "P1", "无依据报障", refs)
            assert r["ok"] is False and r["error"] == "no_evidence", refs

    def test_rejects_unknown_severity(self):
        r = write.ticket_create("db-01", "P9", "x", ["ev_01"])
        assert r["ok"] is False and r["error"] == "bad_severity"

    def test_ticket_id_is_content_hashed_and_stable(self):
        """内置 hash() 按进程加盐，同一工单两次运行拿到不同编号就没法去重——必须用内容哈希。"""
        a = write.ticket_create("db-01", "P0", "同样的内容", ["ev_01"])
        b = write.ticket_create("db-01", "P0", "同样的内容", ["ev_02"])
        assert a["ticket_id"] == b["ticket_id"]
        assert a["ticket_id"].startswith("T")
        c = write.ticket_create("db-01", "P0", "不同的内容", ["ev_01"])
        assert c["ticket_id"] != a["ticket_id"]


class TestNotifySend:
    def test_dry_run_records_but_delivers_nothing(self, settings):
        r = write.notify_send("wecom", "db-01 疑似 OOM", True)
        assert r["ok"] is True and r["dry_run"] is True
        line = json.loads(open(r["path"], encoding="utf-8").read().strip().splitlines()[-1])
        assert line["delivered"] is False
        assert "dry-run" in r["summary"]
        assert r["path"].startswith(settings.ARTIFACT_DIR)

    def test_channel_domain(self):
        for ch in ("wecom", "dingtalk", "email", "console"):
            assert write.notify_send(ch, "x", True)["ok"] is True, ch
        assert write.notify_send("slack", "x", True)["error"] == "bad_channel"

    # 该缺陷已修复，原 xfail 已转为正常断言（见 docs/LIMITATIONS.md 的修复记录）
    def test_non_dry_run_should_not_claim_delivery(self):
        r = write.notify_send("console", "x", False)
        assert "已按配置投递" not in r["summary"]


class TestReportRender:
    def test_markdown_report_has_four_sections(self, settings):
        r = write.report_render("web-01", "web-01 巡检报告", "markdown")
        assert r["ok"] is True and r["sections"] == ["指标水位", "容量预测", "根因分析", "配置体检"]
        text = open(r["path"], encoding="utf-8").read()
        for section in ("一、指标水位", "二、容量预测", "三、根因分析", "四、配置体检"):
            assert section in text
        assert "92" in text and "非真实生产数据" in text
        assert r["path"].endswith(".md") and os.path.dirname(r["path"]) == settings.ARTIFACT_DIR

    def test_text_format_extension(self):
        assert write.report_render("cache-01", "标题", "text")["path"].endswith(".txt")

    def test_bad_format(self):
        assert write.report_render("web-01", "x", "pdf")["error"] == "bad_format"

    def test_same_day_rerun_overwrites_one_file(self):
        a = write.report_render("web-01", "x", "markdown")
        b = write.report_render("web-01", "x", "markdown")
        assert a["path"] == b["path"], "同日重跑覆盖同一份，而不是攒出 30 个近似文件"

    # 该缺陷已修复，原 xfail 已转为正常断言（见 docs/LIMITATIONS.md 的修复记录）
    def test_report_for_unknown_host_should_be_refused(self):
        r = write.report_render("ghost-09", "不存在的主机", "markdown")
        assert r["ok"] is False
        assert not os.path.exists(r["path"])
