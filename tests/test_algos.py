"""算法层单测：BM25 / EWMA / 线性外推 / 日志签名 / 根因评分 / 严重级。

这些是 Agent 的"判断力"来源，所以每个边界都单独钉死：
冷启动不报异常、非单调序列拒绝外推、日均换算不能错 24 倍、
没有词边界的数字（total-vm 12884901kB）必须归一化干净。
"""
from __future__ import annotations

import pytest

from app import data_store
from app.algos import (
    BM25Index,
    aggregate_logs,
    ewma_anomalies,
    linear_forecast,
    log_signature,
    score_hypotheses,
    severity_of,
    tokenize,
)

# 一个带轻微上涨趋势 + 大幅抖动的序列：斜率为正但趋势不可信，必须拒答。
NOISY_FLAT = [40 + 0.1 * i + (5 if i % 3 == 0 else -3 if i % 3 == 1 else 1) for i in range(60)]


class TestTokenize:
    def test_ascii_word_and_number_split(self):
        toks = tokenize("CPU2 62% oom-kill")
        assert "cpu2" in toks
        assert "62" in toks
        assert "oom-kill" in toks

    def test_cjk_unigrams_and_bigrams(self):
        toks = tokenize("磁盘")
        assert toks == ["磁", "磁盘", "盘"]
        assert "盘水" in tokenize("磁盘水位")

    def test_mixed_order_independent_of_case(self):
        assert tokenize("OOM Killer") == tokenize("oom killer")


class TestBM25:
    @pytest.fixture
    def index(self) -> BM25Index:
        idx = BM25Index()
        idx.add("rb-disk", "磁盘水位过高 写满 logrotate 清理 容量", {"title": "磁盘"})
        idx.add("rb-oom", "进程被 OOM Killer 终止 内存不足 重启", {"title": "OOM"})
        idx.add("rb-mq", "消息队列堆积 消费 prefetch unacked", {"title": "MQ"})
        return idx

    def test_ranking_is_semantically_right(self, index):
        ranked = index.score("磁盘 写满")
        assert ranked[0][0] == "rb-disk"
        # 两个词都只出现在磁盘手册里 → 其他手册得 0 分，这是"检索没命中就不硬凑"的下限
        assert len(ranked) == 1
        assert ranked[0][1] > 0

    def test_ambiguous_query_ranks_by_relevance(self, index):
        ranked = dict(index.score("内存 重启 OOM"))
        assert ranked["rb-oom"] == max(ranked.values())

    def test_scores_sorted_descending(self, index):
        scores = [s for _, s in index.score("内存 重启 OOM")]
        assert scores == sorted(scores, reverse=True)
        assert all(s > 0 for s in scores)

    def test_unseen_term_contributes_nothing(self, index):
        assert index.score("zzzqqq") == []

    def test_empty_index_returns_empty(self):
        assert BM25Index().score("磁盘") == []

    def test_longer_doc_is_penalized_by_length_norm(self):
        """同一关键词，短文档得分更高（BM25 的长度归一化真的生效了）。"""
        idx = BM25Index(k1=1.5, b=0.75)
        idx.add("short", "磁盘")
        idx.add("long", "磁盘 而且这一篇还写了非常多其他无关的内容用来稀释词频")
        ranked = idx.score("磁盘")
        assert ranked[0][0] == "short"

    def test_meta_and_size(self, index):
        assert index.size() == 3
        assert index.meta("rb-oom")["title"] == "OOM"
        assert index.meta("ghost") == {}

    def test_rare_term_outweighs_common_term(self):
        """IDF 生效：只有一篇文档含有的稀有词，应当压过四篇文档都含有的常见词。"""
        idx = BM25Index()
        idx.add("target", "常见 稀有")
        for i in range(3):
            idx.add(f"noise{i}", "常见 别的")
        ranked = idx.score("常见 稀有")
        assert ranked[0][0] == "target"
        assert idx._df["稀有"] == 1 and idx._df["常见"] == 4


class TestEwmaAnomalies:
    def test_cold_start_never_flags(self):
        """前 min_points 个点只建基线：启动尖峰不能报异常（冷启动误报是运维工具的耻辱）。"""
        out = ewma_anomalies([100.0, 10, 10, 10, 10, 10], min_points=5)
        assert out["triggered"] == []
        assert out["n"] == 6

    def test_shorter_than_min_points_has_no_baseline(self):
        out = ewma_anomalies([1, 2, 3, 4], min_points=5)
        assert out == {"triggered": [], "baseline": None, "upper_control": None, "n": 4}

    def test_spike_after_warmup_is_flagged(self):
        out = ewma_anomalies([10.0] * 6 + [100.0] + [10.0], min_points=5)
        assert [t["index"] for t in out["triggered"]] == [6]
        assert out["triggered"][0]["value"] == 100.0
        assert out["triggered"][0]["ucl"] >= out["triggered"][0]["baseline"]

    def test_stable_series_has_no_alerts(self):
        assert ewma_anomalies([10.0] * 40, min_points=5)["triggered"] == []

    def test_monotonic_ramp_does_not_alert(self):
        """EWMA 跟着趋势走：单调上涨的水位不该被当成异常点刷屏（这正是选它而不是 z-score 的理由）。"""
        ramp = [62.0 + 0.43 * h for h in range(72)]
        assert ewma_anomalies(ramp, alpha=0.3, k_sigma=3.0, min_points=5)["triggered"] == []

    def test_seed_web01_disk_flags_far_fewer_than_global_zscore(self, seeded_dataset):
        rows = [r for r in data_store.metrics() if r["host"] == "web-01"]
        series = [float(r["disk"]) for r in rows]
        assert len(ewma_anomalies(series)["triggered"]) <= 1


class TestLinearForecast:
    def test_monotonic_ramp_is_trustworthy(self):
        y = [50.0 + 0.5 * i for i in range(40)]
        fc = linear_forecast(y, threshold=90.0, points_per_day=1)
        assert fc["r2"] == pytest.approx(1.0)
        assert fc["confidence"] == "high"
        assert fc["slope_per_step"] == pytest.approx(0.5)
        assert fc["days_to_threshold"] == pytest.approx(41.0)

    def test_noisy_flat_series_refuses_to_extrapolate(self):
        fc = linear_forecast(NOISY_FLAT, threshold=85.0, points_per_day=1)
        assert fc["confidence"] == "low"
        assert fc["r2"] < 0.6
        assert fc["slope_per_step"] > 0, "斜率确实为正，说明拒绝是因为不可信而不是因为没涨"
        assert fc["days_to_threshold"] is None, "非单调序列给出具体天数就是骗人"
        assert "拒绝外推" in fc["note"]

    def test_points_per_day_conversion(self):
        """单位错是最容易被问穿的错：每步斜率 × 24 才是日均增长。"""
        y = [50.0 + 0.5 * i for i in range(40)]
        per_step = linear_forecast(y, 90.0, points_per_day=1)
        per_day = linear_forecast(y, 90.0, points_per_day=24)
        assert per_day["slope_per_day"] == pytest.approx(per_step["slope_per_step"] * 24)
        assert per_day["slope_per_day"] == pytest.approx(12.0)
        assert per_day["points_per_day"] == 24
        assert per_day["days_to_threshold"] == pytest.approx(
            per_step["days_to_threshold"] / 24, abs=0.01
        )

    def test_points_per_day_floor_is_one(self):
        y = [50.0 + 0.5 * i for i in range(40)]
        fc = linear_forecast(y, 90.0, points_per_day=0)
        assert fc["slope_per_day"] == pytest.approx(0.5)

    def test_already_past_threshold_reports_zero_days(self):
        y = [80.0 + 0.5 * i for i in range(20)]
        fc = linear_forecast(y, threshold=85.0, points_per_day=1)
        assert fc["days_to_threshold"] == 0.0

    def test_flat_series_zero_slope(self):
        fc = linear_forecast([50.0, 50.0], 85.0)
        assert fc["slope_per_step"] == 0.0
        assert fc["days_to_threshold"] is None

    def test_single_point_declines_to_answer(self):
        fc = linear_forecast([50.0], 85.0)
        assert fc["confidence"] == "none"
        assert fc["days_to_threshold"] is None

    def test_seed_web01_disk_forecast_is_deterministic(self, seeded_dataset):
        rows = [r for r in data_store.metrics() if r["host"] == "web-01"]
        series = [float(r["disk"]) for r in rows][-72:]
        fc = linear_forecast(series, 85.0, points_per_day=24)
        assert fc["confidence"] == "high"
        assert fc["r2"] > 0.99
        assert fc["current"] == pytest.approx(92.47, abs=0.1)
        assert fc["days_to_threshold"] == 0.0, "已经越过告警线，属于当前故障而不是预测"


class TestLogSignature:
    def test_digits_glued_to_unit_are_masked(self):
        """关键用例：total-vm 12884901kB 里数字紧贴单位，\b 匹配不到，必须用无边界正则。"""
        a = log_signature("Out of memory: Killed process 21733 (mysqld) total-vm 12884901kB")
        b = log_signature("Out of memory: Killed process 99999 (mysqld) total-vm 5551234kB")
        assert a == b
        assert "<N>kB" in a, a
        assert "12884901" not in a

    def test_timestamp_ip_uuid_path_are_normalized(self):
        sig = log_signature("2026-10-16T03:04:05+08:00 id 123e4567-e89b-12d3-a456-426614174000 "
                            "from 10.12.3.11:5672 file /var/log/nginx/access.log")
        assert "<TS>" in sig
        assert "<UUID>" in sig
        assert "<IP>" in sig
        assert "<PATH>" in sig
        assert "10.12.3.11" not in sig

    def test_host_port_pair_still_has_numbers_masked(self):
        sig = log_signature("read timed out after 9000ms calling http://mq-01:5672 enqueue")
        assert sig == "read timed out after <N>ms calling http://mq-<N>:<N> enqueue"

    def test_signature_is_truncated(self):
        assert len(log_signature("x" * 300)) == 160

    def test_aggregate_counts_by_template(self):
        lines = [
            {"message": "cannot get connection from pool, wait 5000ms", "level": "ERROR",
             "service": "app", "ts": "2026-10-16T01:00:00"},
            {"message": "cannot get connection from pool, wait 6000ms", "level": "ERROR",
             "service": "app", "ts": "2026-10-16T02:00:00"},
            {"message": "Too many connections", "level": "ERROR", "service": "mysqld",
             "ts": "2026-10-16T03:00:00"},
        ]
        top = aggregate_logs(lines, top=5)
        assert top[0]["count"] == 2, "同一模板的两次抖动必须合成一个签名"
        assert top[0]["first_ts"] == "2026-10-16T01:00:00"
        assert top[0]["last_ts"] == "2026-10-16T02:00:00"
        assert top[1]["count"] == 1
        assert len(aggregate_logs(lines, top=1)) == 1

    def test_aggregate_empty_input(self):
        assert aggregate_logs([]) == []


class TestScoreHypotheses:
    WEIGHTS = {"metric": 1.0, "log": 0.95, "config": 0.8, "doc": 0.6}

    def test_multiple_evidence_beats_single_strong_one(self):
        hs = [
            {"cause": "a", "refs": ["log"], "strength": 0.9},
            {"cause": "b", "refs": ["metric", "log"], "strength": 0.5},
        ]
        out = score_hypotheses(hs, self.WEIGHTS)
        assert [h["cause"] for h in out] == ["b", "a"]
        assert out[0]["score"] == pytest.approx(0.975)
        assert out[1]["score"] == pytest.approx(0.855)

    def test_score_is_normalized_by_best_evidence_not_by_count(self):
        """一条 log 证据 + 强度 0.9 → 0.855 直接映射到 P0，不能被除以 4 统统变 P3。"""
        out = score_hypotheses([{"cause": "x", "refs": ["log"], "strength": 0.9}], self.WEIGHTS)
        assert out[0]["score"] == pytest.approx(0.855)
        assert severity_of(out[0]["score"]) == "P0"

    def test_score_capped_at_one(self):
        out = score_hypotheses([{"cause": "cap", "refs": ["metric", "metric"], "strength": 1.0}],
                               self.WEIGHTS)
        assert out[0]["score"] == 1.0

    def test_unknown_evidence_kind_scores_zero(self):
        out = score_hypotheses([{"cause": "x", "refs": ["nonsense"], "strength": 1.0}], self.WEIGHTS)
        assert out[0]["score"] == 0.0

    def test_empty_weights_does_not_crash(self):
        out = score_hypotheses([{"cause": "x", "refs": ["metric"], "strength": 0.5}], {})
        assert out[0]["score"] == 0.0
        assert out[0]["probability"] == 0.0

    def test_probability_is_relative_share(self):
        hs = [{"cause": f"c{i}", "refs": ["metric"], "strength": s} for i, s in enumerate([1.0, 0.5])]
        out = score_hypotheses(hs, self.WEIGHTS)
        assert out[0]["probability"] + out[1]["probability"] == pytest.approx(1.0, abs=0.01)
        assert out[0]["probability"] > out[1]["probability"]

    def test_tie_broken_by_cause_name(self):
        hs = [{"cause": "zz", "refs": ["metric"], "strength": 0.5},
              {"cause": "aa", "refs": ["metric"], "strength": 0.5}]
        assert [h["cause"] for h in score_hypotheses(hs, self.WEIGHTS)] == ["aa", "zz"]

    def test_runbook_and_evidence_are_carried_through(self):
        out = score_hypotheses([{"cause": "x", "refs": ["log"], "strength": 0.9,
                                 "runbook": "rb-oom-kill"}], self.WEIGHTS)
        assert out[0]["runbook"] == "rb-oom-kill"
        assert out[0]["supporting_evidence"] == ["log"]

    def test_db01_seed_oom_ranks_first(self, seeded_dataset):
        """评测集里 (b) 用例的地基：db-01 的 OOM 假设必须排在第一。"""
        from app.tools import ops

        rc = ops.root_cause_rank("db-01", 24)
        assert rc["ok"] is True
        assert "OOM" in rc["hypotheses"][0]["cause"]
        assert rc["severity"] == "P0"
        assert rc["top_runbook"] == "rb-oom-kill"


class TestSeverityOf:
    @pytest.mark.parametrize(
        ("score", "expected"),
        [(1.0, "P0"), (0.8, "P0"), (0.799, "P1"), (0.6, "P1"), (0.599, "P2"),
         (0.4, "P2"), (0.399, "P3"), (0.0, "P3")],
    )
    def test_boundaries(self, score, expected):
        assert severity_of(score) == expected
