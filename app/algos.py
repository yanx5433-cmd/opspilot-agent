"""算法层：全部纯函数、确定性、可单测。没有一次 LLM 调用，没有随机性。

放在一个模块里的理由：这些是 Agent 的"判断力"来源，必须能被单独拿出去讲、
单独被测试钉死。规划器可以换、Prompt 可以调，但这几个算法是稳定资产。
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

_CJK = re.compile(r"[一-鿿]")
_ASCII_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*|\d+")


# --------------------------------------------------------------------------
# 检索：BM25（中英混排，中文按 bigram 切）
# --------------------------------------------------------------------------
def tokenize(text: str) -> list[str]:
    """ASCII 词 + 数字 + 中文 bigram。

    为什么不用 jieba：多一个依赖，且运维手册的关键词（"磁盘水位""连接池耗尽"）
    分词好坏对最终排序的影响，远不如 bigram 稳定。bigram 召回高、可预测、零依赖。
    """
    text = text.lower()
    tokens = _ASCII_WORD.findall(text)
    cjk_chars = [c for c in text if _CJK.search(c)]
    for i in range(len(cjk_chars)):
        tokens.append(cjk_chars[i])
        if i + 1 < len(cjk_chars):
            tokens.append(cjk_chars[i] + cjk_chars[i + 1])
    return tokens


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: dict[str, Counter[str]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._df: Counter[str] = Counter()
        self._lens: dict[str, int] = {}
        self._avg: float = 0.0

    def add(self, doc_id: str, text: str, meta: dict[str, Any] | None = None) -> None:
        tf = Counter(tokenize(text))
        self._docs[doc_id] = tf
        self._meta[doc_id] = meta or {}
        self._lens[doc_id] = sum(tf.values()) or 1
        for term in tf:
            self._df[term] += 1
        total = sum(self._lens.values())
        self._avg = total / len(self._lens) if self._lens else 1.0

    def score(self, query: str) -> list[tuple[str, float]]:
        n = len(self._docs)
        if n == 0:
            return []
        q_terms = set(tokenize(query))
        out: dict[str, float] = defaultdict(float)
        for term in q_terms:
            df = self._df.get(term, 0)
            if df == 0:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for doc_id, tf in self._docs.items():
                f = tf.get(term, 0)
                if not f:
                    continue
                norm = f * (self.k1 + 1) / (
                    f + self.k1 * (1 - self.b + self.b * self._lens[doc_id] / self._avg)
                )
                out[doc_id] += idf * norm
        return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))

    def meta(self, doc_id: str) -> dict[str, Any]:
        return self._meta.get(doc_id, {})

    def size(self) -> int:
        return len(self._docs)


# --------------------------------------------------------------------------
# 异常检测：EWMA（指数加权移动平均）+ k·σ 控制限
# --------------------------------------------------------------------------
def ewma_anomalies(
    series: Iterable[float], alpha: float = 0.3, k_sigma: float = 3.0, min_points: int = 5
) -> dict[str, Any]:
    """返回基线、控制限与越限点。

    为什么不用 z-score：巡检指标有明显趋势（磁盘水位单调上涨），
    全局均值会把"缓慢变坏"稀释掉。EWMA 跟着趋势走，越限判定才有意义。
    前 min_points 个点只建基线不报异常，避免冷启动误报。
    """
    values = [float(v) for v in series]
    if len(values) < min_points:
        return {"triggered": [], "baseline": None, "upper_control": None, "n": len(values)}
    level = values[0]
    var = 0.0
    flagged: list[dict[str, Any]] = []
    for i, v in enumerate(values):
        if i >= min_points:
            ucl = level + k_sigma * math.sqrt(max(var, 1e-9))
            if v > ucl:
                flagged.append({"index": i, "value": round(v, 2), "baseline": round(level, 2), "ucl": round(ucl, 2)})
        diff = v - level
        level = alpha * v + (1 - alpha) * level
        var = (1 - alpha) * (var + alpha * diff * diff)
    return {
        "triggered": flagged,
        "baseline": round(level, 2),
        "upper_control": round(level + k_sigma * math.sqrt(max(var, 1e-9)), 2),
        "n": len(values),
    }


# --------------------------------------------------------------------------
# 容量预测：最小二乘线性回归外推"多少天后写满"
# --------------------------------------------------------------------------
def linear_forecast(
    y: list[float], threshold: float, horizon_days: int = 30, points_per_day: int = 1
) -> dict[str, Any]:
    """y 为等间隔采样的水位序列，返回斜率/R²/预计触顶天数。

    points_per_day 负责把"每步"换算成"每天"：巡检指标是小时级采样，
    直接拿每步斜率讲"日均增长"会低估 24 倍——单位错是最容易被问穿的错。
    R² 低于 0.6 时明确拒答（confidence=low, days_to_threshold=None）：
    非单调抖动序列外推出来的"8 天后写满"是骗人的，宁可不说。
    """
    n = len(y)
    if n < 2:
        return {"slope_per_step": None, "slope_per_day": None, "r2": None,
                "days_to_threshold": None, "confidence": "none"}
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(y) / n
    sxy = sum((xs[i] - mx) * (y[i] - my) for i in range(n))
    sxx = sum((xs[i] - mx) ** 2 for i in range(n))
    if sxx == 0:
        return {"slope_per_step": 0.0, "slope_per_day": 0.0, "r2": 1.0,
                "days_to_threshold": None, "confidence": "low"}
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_res = sum((y[i] - (intercept + slope * xs[i])) ** 2 for i in range(n))
    ss_tot = sum((v - my) ** 2 for v in y) or 1e-9
    r2 = 1 - ss_res / ss_tot
    per_day = slope * max(points_per_day, 1)
    days: float | None = None
    if per_day > 0:
        need = threshold - (intercept + slope * (n - 1))
        days = round(need / per_day, 2) if need > 0 else 0.0
    confidence = "high" if r2 >= 0.85 else ("medium" if r2 >= 0.6 else "low")
    return {
        "slope_per_step": round(slope, 5),
        "slope_per_day": round(per_day, 4),
        "intercept": round(intercept, 3),
        "r2": round(r2, 4),
        "current": round(y[-1], 2),
        "points_per_day": points_per_day,
        "horizon_days": horizon_days,
        "days_to_threshold": days if confidence != "low" else None,
        "confidence": confidence,
        "note": (
            "外推可信（趋势单调）"
            if confidence != "low"
            else f"序列非单调，拒绝外推（R²={r2:.3f} < 0.6）"
        ),
    }


# --------------------------------------------------------------------------
# 日志：把 "id=8231 elapsed=1234ms" 归一化成模板，才能算出"同一错误刷了 300 次"
# --------------------------------------------------------------------------
_MASKS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"), "<TS>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    (re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"/(?:[\w.\-]+/){2,}[\w.\-]+"), "<PATH>"),
    # 不带词边界：日志里的 "total-vm 12884901kB" 这种数字紧贴单位，
    # 用 \b 匹配不到，模板就归一化不干净、同一错误会被拆成好几个签名。
    (re.compile(r"\d+(?:\.\d+)?"), "<N>"),
]


def log_signature(line: str) -> str:
    sig = line.strip()
    for pat, rep in _MASKS:
        sig = pat.sub(rep, sig)
    return sig[:160]


def aggregate_logs(lines: list[dict[str, Any]], top: int = 5) -> list[dict[str, Any]]:
    """按签名聚合，返回出现次数最多的错误模式（附样本与首次时间）。"""
    buckets: dict[str, dict[str, Any]] = {}
    for item in lines:
        sig = log_signature(item.get("message", ""))
        b = buckets.setdefault(
            sig,
            {"signature": sig, "count": 0, "first_ts": item.get("ts"), "last_ts": item.get("ts"),
             "level": item.get("level", ""), "service": item.get("service", ""), "sample": item.get("message", "")},
        )
        b["count"] += 1
        b["last_ts"] = item.get("ts") or b["last_ts"]
    ranked = sorted(buckets.values(), key=lambda b: (-b["count"], b["signature"]))
    return ranked[:top]


# --------------------------------------------------------------------------
# 根因评分：症状证据 → 假设排序（可解释，不是黑盒）
# --------------------------------------------------------------------------
def score_hypotheses(
    hypotheses: list[dict[str, Any]], evidence_weight: dict[str, float]
) -> list[dict[str, Any]]:
    """每个假设带若干证据引用与强度 0~1，加权求和后归一化成排序。

    hypotheses: [{"cause": "...", "refs": ["metric"], "strength": 0.9}, ...]
    evidence_weight: {"metric": 1.0, "log": 0.95, ...}   # 证据可靠性（实时指标 > 文档）

    归一化基准取"最可靠证据种类的单条权重"，因此分数天然落在 0~1：
    一条 log 证据支撑、强度 0.9 的假设得 0.855 → P0，而不是被除以 4 之后统统变 P3。
    分数只做排序与严重级映射，不代表真实概率；probability 是相对占比，报告里要这么解释。
    """
    max_w = max(evidence_weight.values()) if evidence_weight else 1.0
    scored = []
    for h in hypotheses:
        raw = sum(evidence_weight.get(r, 0.0) * float(h.get("strength", 0.5)) for r in h.get("refs", []))
        scored.append(
            {
                "cause": h["cause"],
                "score": round(min(1.0, raw / max_w) if max_w else 0.0, 3),
                "supporting_evidence": h.get("refs", []),
                "runbook": h.get("runbook", ""),
            }
        )
    scored.sort(key=lambda x: (-x["score"], x["cause"]))
    total = sum(s["score"] for s in scored) or 1.0
    for s in scored:
        s["probability"] = round(s["score"] / total, 3)
    return scored


def severity_of(score: float) -> str:
    return "P0" if score >= 0.8 else ("P1" if score >= 0.6 else ("P2" if score >= 0.4 else "P3"))
