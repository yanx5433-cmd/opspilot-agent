"""只读工具集：Agent 观察世界的 9 只手（另有 kb 2 只、写操作 3 只）。

约定：每个工具返回 {"ok", "summary", "evidence_kind", "source", **data}。
summary 是要被写进结论的一手判断，source 让每条结论都能回溯到具体主机/文件——
"证据可追溯"在代码里的落点就是这个 source 字段，不是文档里的一句话。

架构性安全：没有 subprocess、没有 os.system、没有 eval。Agent 拿不到 shell，
所以护栏拦的是"越权意图"和"参数污染"，而不必假设有一个可被注入的执行面。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from .. import data_store
from ..algos import aggregate_logs, ewma_anomalies, linear_forecast, score_hypotheses, severity_of
from ..config import get_settings

VALID_METRICS = ("cpu", "mem", "disk", "io_wait", "load", "net_in_mbps")
VALID_LEVELS = ("", "DEBUG", "INFO", "WARN", "ERROR", "FATAL")
LEVEL_ORDER = ("FATAL", "ERROR", "WARN", "INFO", "DEBUG")
# level 参数按"下限"解释（运维工具的通行语义）：查 ERROR 必须连 FATAL 一起命中，
# 否则 OOM 这类 FATAL 日志会被漏掉——第一版真实跑诊断时就踩过这个坑。
LEVEL_RANK = {lv: i for i, lv in enumerate(reversed(LEVEL_ORDER))}


def result(summary: str, kind: str, source: str, ok: bool = True, **data: Any) -> dict[str, Any]:
    return {"ok": ok, "summary": summary, "evidence_kind": kind, "source": source, **data}


def _tail(rows: list[dict[str, Any]], host: str, n: int) -> list[dict[str, Any]]:
    picked = [r for r in rows if r.get("host") == host]
    picked.sort(key=lambda r: r["ts"])
    return picked[-n:] if n and n > 0 else picked


def _unknown_host(host: str) -> str | None:
    if host in data_store.host_ids():
        return None
    return f"主机 {host!r} 不在巡检清单（可用：{', '.join(data_store.host_ids())}）"


def _series(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [float(r[key]) for r in rows if key in r]


# ---------------------------------------------------------------- metrics
def metrics_query(host: str, window_hours: int, metric: str) -> dict[str, Any]:
    """拉单机指标时序，做 EWMA 异常检测并给趋势判定。"""
    err = _unknown_host(host)
    if err:
        return result(err, "metric", f"metrics:{host}", ok=False, error="unknown_host")
    if metric not in VALID_METRICS:
        return result(f"不支持的指标 {metric!r}（可选：{', '.join(VALID_METRICS)}）",
                      "metric", f"metrics:{host}", ok=False, error="bad_metric")
    s = get_settings()
    window = max(3, min(int(window_hours), 168))
    rows = _tail(data_store.metrics(), host, window)
    series = _series(rows, metric)
    if not series:
        return result(f"{host} 在窗口内无 {metric} 数据", "metric", f"metrics:{host}",
                      ok=False, error="no_data")
    anom = ewma_anomalies(series, s.EWMA_ALPHA, s.EWMA_SIGMA)
    latest, peak, mean = series[-1], max(series), sum(series) / len(series)
    warn_at = {"cpu": s.CPU_WARN, "mem": s.MEM_WARN, "disk": s.DISK_WARN}.get(metric)
    span = series[-1] - series[0]
    trend = "上升" if span > 2 else ("下降" if span < -2 else "平稳")
    flags: list[str] = []
    if warn_at is not None and latest >= warn_at:
        flags.append(f"当前 {latest:.1f}% 已超阈值 {warn_at:.0f}%")
    if anom["triggered"]:
        flags.append(f"EWMA 控制限越限 {len(anom['triggered'])} 点")
    verdict = "、".join(flags) if flags else f"当前 {latest:.1f}%，趋势{trend}，未见越限"
    summary = (f"{host} {metric}：{verdict}（窗口 {window}h，峰值 {peak:.1f}%，均值 {mean:.1f}%）")
    return result(summary, "metric", f"metrics:{host}:{metric}",
                  host=host, metric=metric, window_hours=window, latest=round(latest, 2),
                  peak=round(peak, 2), mean=round(mean, 2), trend=trend,
                  anomalies=anom["triggered"][:5], baseline=anom["baseline"],
                  threshold=warn_at, points=len(series))


# ---------------------------------------------------------------- logs
def log_search(service: str, pattern: str, level: str, limit: int) -> dict[str, Any]:
    """按服务/关键词/级别下限检索日志，并把同类错误归一化成模板计数。

    level 是**下限**：传 ERROR 会同时命中 FATAL。
    """
    rows = data_store.logs()
    level = (level or "").upper().strip()
    if level not in VALID_LEVELS:
        return result(f"非法日志级别 {level!r}（可选：{', '.join(filter(None, VALID_LEVELS))} 或留空）",
                      "log", "logs:search", ok=False, error="bad_level")
    hit = rows
    if service:
        needle = service.lower()
        hit = [r for r in hit if needle in str(r.get("service", "")).lower()]
    if level:
        floor = LEVEL_RANK[level]
        hit = [r for r in hit if LEVEL_RANK.get(r.get("level", ""), -1) >= floor]
    if pattern:
        rx: re.Pattern[str] | None
        try:
            rx = re.compile(pattern, re.I)
        except re.error:
            rx = None
        if rx is not None:
            hit = [r for r in hit if rx.search(str(r.get("message", "")))]
        else:
            low = pattern.lower()
            hit = [r for r in hit if low in str(r.get("message", "")).lower()]
    by_level = {lv: sum(1 for r in hit if r.get("level") == lv) for lv in LEVEL_ORDER}
    by_level = {k: v for k, v in by_level.items() if v}
    top = aggregate_logs(hit, top=max(1, min(int(limit), 10)))
    hosts = sorted({str(r.get("host", "?")) for r in hit})
    if not hit:
        return result(f"未匹配到日志（service={service or '全部'}, pattern={pattern or '无'}, "
                      f"level={level or '全部'}）", "log", "logs:search",
                      total=0, signatures=[], hosts=[], by_level={})
    lead = top[0]
    dist = "、".join(f"{k} {by_level[k]}" for k in LEVEL_ORDER if k in by_level)
    summary = (f"命中 {len(hit)} 条（{dist}），最高频模式「{lead['signature'][:56]}」"
               f"出现 {lead['count']} 次，涉及主机 {', '.join(hosts)}")
    return result(summary, "log", f"logs:{service or 'all'}:{pattern or 'any'}",
                  total=len(hit), signatures=top, hosts=hosts, by_level=by_level,
                  window={"first": hit[0]["ts"], "last": hit[-1]["ts"]})


# ---------------------------------------------------------------- services
def service_status(host: str) -> dict[str, Any]:
    """查单机服务/端口状态与 24h 重启次数。"""
    err = _unknown_host(host)
    if err:
        return result(err, "probe", f"services:{host}", ok=False, error="unknown_host")
    items = data_store.services().get(host, [])
    if not items:
        return result(f"{host} 无服务登记", "probe", f"services:{host}", ok=False, error="no_services")
    down = [i for i in items if i.get("status") != "up"]
    flapping = [i for i in items if int(i.get("restarts_24h", 0)) >= 3]
    if not down and not flapping:
        verdict = "全部在线"
    else:
        parts = []
        if down:
            parts.append(f"{len(down)} 个离线（{', '.join(i['name'] for i in down)}）")
        if flapping:
            parts.append(f"{len(flapping)} 个频繁重启（{', '.join(i['name'] for i in flapping)}）")
        verdict = "、".join(parts)
    return result(f"{host} 共 {len(items)} 个服务：{verdict}", "probe", f"services:{host}",
                  host=host, services=items, down=[i["name"] for i in down],
                  flapping=[i["name"] for i in flapping])


# ---------------------------------------------------------------- config
LintRule = tuple[str, Callable[[Any], bool], str, str]
LINT_RULES: list[LintRule] = [
    ("cert_expire_days", lambda v: isinstance(v, int) and v <= 30, "P1", "证书 {v} 天后到期，需立即续期"),
    # 距到期还远 ≠ 没纳入监控。长期未纳入巡检的证书才是半夜炸掉的那种。
    ("cert_expire_days", lambda v: isinstance(v, int) and v > 365, "P2", "证书剩余 {v} 天，超出巡检窗口，建议纳入续期自动化"),
    ("backup_enabled", lambda v: v is False, "P1", "备份开关为关，故障时无数据可恢复"),
    ("retry", lambda v: isinstance(v, int) and v == 0, "P2", "接口无重试（retry=0），瞬时抖动会直接失败"),
    ("read_timeout_ms", lambda v: isinstance(v, int) and v > 8000, "P2", "读超时 {v}ms 过长，慢调用会拖垮线程池"),
    ("jdbc_pool_max", lambda v: isinstance(v, int) and v < 20, "P2", "连接池上限 {v} 偏小，高并发会排队"),
    ("keepalive", lambda v: isinstance(v, int) and v == 0, "P2", "未开启 keepalive，每次请求都付 TCP 握手成本"),
    ("worker_connections", lambda v: isinstance(v, int) and 0 < v < 1024, "P2", "worker_connections={v} 偏低"),
    ("log_level", lambda v: str(v).upper() == "DEBUG", "P2", "生产环境 log_level=DEBUG 会加速磁盘耗尽"),
]


def config_check(host: str, component: str) -> dict[str, Any]:
    """配置体检：只按 lint 规则判定，输出可整改清单。规则数与命中数都返回，便于复核。"""
    host_cfg = data_store.configs().get(host)
    if host_cfg is None:
        return result(f"{host} 无配置记录", "config", f"config:{host}", ok=False, error="unknown_host")
    comps = {k: v for k, v in host_cfg.items() if not component or component.lower() in k.lower()}
    if not comps:
        return result(f"{host} 上找不到组件 {component!r}（现有：{', '.join(host_cfg)}）",
                      "config", f"config:{host}", ok=False, error="unknown_component")
    findings: list[dict[str, Any]] = []
    for comp, kv in comps.items():
        for key, pred, sev, tpl in LINT_RULES:
            if key in kv and pred(kv[key]):
                findings.append({"component": comp, "key": key, "value": kv[key],
                                 "severity": sev, "advice": tpl.format(v=kv[key])})
    p1 = [f for f in findings if f["severity"] == "P1"]
    summary = (f"{host} 配置体检：{len(comps)} 个组件 / {len(LINT_RULES)} 条规则，"
               f"命中 {len(findings)} 项待整改" + (f"，其中 P1 {len(p1)} 项" if p1 else "，无 P1 风险"))
    return result(summary, "config", f"config:{host}", host=host, components=list(comps),
                  findings=findings, p1_count=len(p1), rules_applied=len(LINT_RULES))


# ---------------------------------------------------------------- disk forecast
def disk_forecast(host: str, horizon_days: int) -> dict[str, Any]:
    """磁盘水位线性外推：多少天触及告警线。R² 不达标时明确拒答。"""
    err = _unknown_host(host)
    if err:
        return result(err, "metric", f"disk:{host}", ok=False, error="unknown_host")
    s = get_settings()
    days_window = max(3, min(int(horizon_days), 30))
    rows = _tail(data_store.metrics(), host, 24 * days_window)  # 指标是小时级采样
    series = _series(rows, "disk")
    if len(series) < s.FORECAST_MIN_POINTS:
        return result(f"{host} 磁盘样本仅 {len(series)} 点，低于最小外推要求 {s.FORECAST_MIN_POINTS} 点，拒绝预测",
                      "metric", f"disk:{host}", ok=False, error="insufficient_samples")
    fc = linear_forecast(series, s.DISK_WARN, days_window, points_per_day=24)
    d2t = fc["days_to_threshold"]
    if d2t is None:
        summary = f"{host} 磁盘 {fc['current']:.1f}%，但趋势不可信（R²={fc['r2']}），不给触顶时间"
    elif d2t <= 0:
        summary = f"{host} 磁盘已达 {fc['current']:.1f}%（告警线 {s.DISK_WARN:.0f}%），属于当前问题而非预测"
    else:
        summary = (f"{host} 磁盘 {fc['current']:.1f}%，日均 +{fc['slope_per_day']:.2f}%（R²={fc['r2']}），"
                   f"预计 {int(d2t)} 天后触及 {s.DISK_WARN:.0f}%")
    return result(summary, "metric", f"disk:{host}", host=host, forecast=fc,
                  threshold=s.DISK_WARN, samples=len(series), window_hours=24 * days_window)


# ---------------------------------------------------------------- http probe
_LOOPBACK = re.compile(r"^https?://(localhost|127\.0\.0\.1|0\.0\.0\.0)(:\d+)?(/|$)")


def http_probe(url: str, timeout_ms: int) -> dict[str, Any]:
    """真实 HTTP 探活。SSRF 防护：目标只允许本机回环，或巡检清单内的主机名/IP。"""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return result(f"仅支持 http/https 协议：{url!r}", "probe", f"probe:{url}",
                      ok=False, error="bad_scheme")
    netloc = re.sub(r"^https?://", "", url).split("/")[0]
    bare = netloc.split(":")[0]
    known_ips = {h.get("ip") for h in data_store.hosts().get("hosts", {}).values()}
    allowed = bool(_LOOPBACK.match(url)) or bare in set(data_store.host_ids()) or bare in known_ips
    if not allowed:
        return result(f"探测目标 {netloc} 不在巡检清单，按 SSRF 防护拒绝（仅允许回环地址与清单内主机/IP）",
                      "probe", f"probe:{url}", ok=False, error="not_allowlisted")
    timeout = max(0.5, min(int(timeout_ms), 8000)) / 1000
    t0 = dt.datetime.now()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpsPilot/0.3 (health-probe)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, body = resp.status, resp.read(2048)
        ms = int((dt.datetime.now() - t0).total_seconds() * 1000)
        return result(f"{url} 探活成功：HTTP {status}，耗时 {ms}ms，响应 {len(body)}B",
                      "probe", f"probe:{url}", status=status, latency_ms=ms, bytes=len(body))
    except urllib.error.HTTPError as exc:
        ms = int((dt.datetime.now() - t0).total_seconds() * 1000)
        return result(f"{url} 返回 HTTP {exc.code}（进程活着但服务端报错，耗时 {ms}ms）",
                      "probe", f"probe:{url}", status=exc.code, latency_ms=ms, error="http_error")
    except Exception as exc:
        ms = int((dt.datetime.now() - t0).total_seconds() * 1000)
        text = str(exc).lower()
        kind = "timeout" if "timed out" in text else "conn_refused" if "refused" in text else "unreachable"
        return result(f"{url} 探测失败（{kind} / {type(exc).__name__}），{ms}ms 后放弃",
                      "probe", f"probe:{url}", latency_ms=ms, error=kind)


# ---------------------------------------------------------------- history
def incident_history(host: str, keyword: str) -> dict[str, Any]:
    """查同类历史故障，给 MTTR 基线与"这是不是老毛病"。"""
    rows = data_store.incidents()
    hit = [
        r for r in rows
        if (not host or host in (r.get("host"), "*"))
        and (not keyword or keyword.lower() in json.dumps(r, ensure_ascii=False).lower())
    ]
    if not hit:
        return result(f"无同类历史故障（host={host or '任意'}, kw={keyword or '无'}）",
                      "doc", "history:none", total=0, items=[], avg_mttr_min=None, recurring=[])
    hit.sort(key=lambda r: str(r.get("date", "")), reverse=True)
    mttrs = [int(r["mttr_min"]) for r in hit if isinstance(r.get("mttr_min"), int)]
    recurring = [r for r in hit if int(r.get("recurrence", 0)) >= 2]
    avg = round(sum(mttrs) / len(mttrs), 1) if mttrs else None
    summary = (f"命中 {len(hit)} 条历史故障，平均修复 {avg if avg is not None else '-'} 分钟"
               + (f"；{len(recurring)} 类属反复发生（需根治，不只是再处理一次）" if recurring else ""))
    return result(summary, "doc", f"history:{host or '*'}:{keyword or 'any'}",
                  total=len(hit), items=hit[:5], avg_mttr_min=avg,
                  recurring=[r.get("root_cause", "") for r in recurring])


# ---------------------------------------------------------------- topology
def topology_neighbors(host: str) -> dict[str, Any]:
    """查上下游依赖，用来回答"是它坏了，还是它依赖的东西坏了"。"""
    table = data_store.hosts().get("hosts", {})
    if host not in table:
        return result(f"{host} 不在清单", "doc", f"topo:{host}", ok=False, error="unknown_host")
    info = table[host]
    up = [u for u in info.get("upstream", []) if u in table]
    down = [d for d in info.get("downstream", []) if d in table]
    down_detail = {d: table[d].get("services", []) for d in down}
    summary = (f"{host} 上游 {len(up)} 个（{', '.join(up) or '无'}），"
               f"下游 {len(down)} 个（{', '.join(down) or '无'}）")
    return result(summary, "doc", f"topo:{host}", host=host, upstream=up,
                  downstream=down, downstream_detail=down_detail)


# ---------------------------------------------------------------- root cause
SYMPTOM_RULES: list[tuple[str, str, float, str]] = [
    # (code, 假设标签, 强度, 对应 runbook)
    ("disk_high", "磁盘水位过高（写满前兆）", 0.90, "rb-disk-full"),
    ("oom", "进程被 OOM Killer 终止", 0.90, "rb-oom-kill"),
    ("conn_pool", "数据库连接池耗尽", 0.85, "rb-db-pool"),
    ("cert", "TLS 证书临近过期", 0.75, "rb-cert-expiry"),
    ("cpu_high", "CPU 饱和", 0.70, "rb-cpu-saturation"),
    ("mem_high", "内存压力", 0.70, "rb-memory-pressure"),
    ("timeout", "下游超时引发请求堆积", 0.60, "rb-downstream-timeout"),
    ("log_flood", "单一错误刷屏（噪声放大风险）", 0.40, "rb-disk-full"),
]


def _count_hits(rows: list[dict[str, Any]], pattern: str) -> int:
    rx = re.compile(pattern, re.I)
    return sum(1 for r in rows if rx.search(str(r.get("message", ""))))


def root_cause_rank(host: str, lookback_hours: int) -> dict[str, Any]:
    """融合指标/日志/配置三类证据，算出根因假设排序。

    分数由规则算出来，不由模型编出来——这是"可解释诊断"的关键：
    LLM 若参与，只允许把排序翻译成中文结论，不允许改动排序本身。
    """
    err = _unknown_host(host)
    if err:
        return result(err, "doc", f"rc:{host}", ok=False, error="unknown_host")
    s = get_settings()
    win = max(3, min(int(lookback_hours), 168))
    mrows = _tail(data_store.metrics(), host, win)
    if not mrows:
        return result(f"{host} 在 {win}h 窗口内无指标，无法归因", "doc", f"rc:{host}",
                      ok=False, error="no_data")
    lrows = [r for r in data_store.logs() if r.get("host") == host]
    cfg = config_check(host, "")

    disk = _series(mrows, "disk")
    cpu = _series(mrows, "cpu")
    mem = _series(mrows, "mem")
    signals = {
        "metrics": {
            "disk_latest": round(disk[-1], 2), "cpu_latest": round(cpu[-1], 2),
            "mem_latest": round(mem[-1], 2),
            "disk_anomaly_points": len(ewma_anomalies(disk, s.EWMA_ALPHA, s.EWMA_SIGMA)["triggered"]),
            "cpu_anomaly_points": len(ewma_anomalies(cpu, s.EWMA_ALPHA, s.EWMA_SIGMA)["triggered"]),
        },
        "logs": {
            "oom": _count_hits(lrows, r"out of memory|OOM|Killed process"),
            "pool_exhausted": _count_hits(lrows, r"connection pool|pool exhausted|cannot get connection|Too many connections"),
            "timeout": _count_hits(lrows, r"timeout|timed out"),
            "error_total": sum(1 for r in lrows if r.get("level") in {"ERROR", "FATAL"}),
            "top_signature_count": max([b["count"] for b in aggregate_logs(lrows, top=1)] or [0]),
        },
        "config": {"p1_count": cfg.get("p1_count", 0) if cfg["ok"] else 0},
    }
    m, lg, c = signals["metrics"], signals["logs"], signals["config"]
    fired = {
        "disk_high": m["disk_latest"] >= s.DISK_WARN,
        "cpu_high": m["cpu_latest"] >= s.CPU_WARN,
        "mem_high": m["mem_latest"] >= s.MEM_WARN,
        "oom": lg["oom"] > 0,
        "conn_pool": lg["pool_exhausted"] > 0,
        "cert": c["p1_count"] > 0 and cfg["ok"] and any("证书" in f["advice"] for f in cfg["findings"]),
        "timeout": lg["timeout"] > 0,
        "log_flood": lg["top_signature_count"] >= 50,
    }
    evidence_kind = {"disk_high": "metric", "cpu_high": "metric", "mem_high": "metric",
                     "oom": "log", "conn_pool": "log", "timeout": "log",
                     "cert": "config", "log_flood": "log"}
    weight = {"metric": 1.0, "log": 0.95, "config": 0.8, "doc": 0.6}
    hypotheses = []
    for code, label, strength, rb in SYMPTOM_RULES:
        if not fired.get(code):
            continue
        hypotheses.append({"cause": label, "refs": [evidence_kind[code]],
                           "strength": strength, "runbook": rb})
    ranked = score_hypotheses(hypotheses, weight)
    top = ranked[0] if ranked else None
    sev = severity_of(top["score"]) if top else "P3"
    summary = (f"{host} 根因排序：最可能「{top['cause']}」（概率 {top['probability']:.0%}，"
               f"{sev}，共 {len(ranked)} 个候选）" if top
               else f"{host} 未发现可归因的异常证据（指标、日志、配置三类均未触线）")
    return result(summary, "doc", f"rc:{host}", host=host, hypotheses=ranked,
                  signals=signals, top_cause=top["cause"] if top else None,
                  top_probability=top["probability"] if top else 0.0,
                  top_runbook=top["runbook"] if top else "", severity=sev,
                  fired_rules=[k for k, v in fired.items() if v])
