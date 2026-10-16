"""规划器：目标 → 带依赖的步骤序列。双引擎，规则优先。

- rule 引擎：意图识别 + 槽位填充 + 计划模板。确定性、零成本、可单测，是评测基线；
- llm 引擎：把工具清单交给模型，要求返回 JSON 计划，然后**逐条校验**
  （未知工具丢弃、超预算截断、依赖索引重映射成步骤 id）。校验失败即回落规则。

为什么规则仍然是默认引擎：无人值守巡检要求"模型 API 挂了，今晚的巡检照样跑完"。
LLM 是增强项，不是依赖项。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .. import data_store
from ..config import get_settings
from .llm import LlmClient
from .models import Step, new_id
from .reflection import detect_intent
from .tools_registry import ToolRegistry

METRIC_ALIASES = {
    "磁盘": "disk", "硬盘": "disk", "空间": "disk", "disk": "disk",
    "cpu": "cpu", "处理器": "cpu", "负载": "load", "load": "load",
    "内存": "mem", "mem": "mem", "网络": "net_in_mbps",
}

# 症状词 → (日志检索正则, 首选指标)。自然语言里"重启/挂了/慢"才是有效检索词，
# 整句问话拿去 when 做日志 pattern 必然 0 命中——这是第一版真实跑出来的缺陷。
SYMPTOM_PATTERNS: list[tuple[str, str, str]] = [
    ("重启", r"restart|reboot|Killed|OOM|killed process", "mem"),
    ("OOM", r"out of memory|OOM|Killed process", "mem"),
    ("内存", r"memory|OOM|out of memory", "mem"),
    ("磁盘", r"disk|space|errno=28|No space", "disk"),
    ("写满", r"disk|No space|errno=28", "disk"),
    ("超时", r"timeout|timed out", "cpu"),
    ("慢", r"slow|timed out|p99", "cpu"),
    ("堆积", r"queue|backlog|unacked|prefetch", "cpu"),
    ("连接", r"connection|pool|Too many connections", "cpu"),
    ("CPU", r"cpu|load", "cpu"),
    ("报错", r"ERROR|FATAL", "cpu"),
    ("挂", r"down|refused|connection closed", "cpu"),
]
_STOPWORDS = re.compile(
    r"(为什么|怎么样|怎么|帮忙?|请|麻烦|一直|老是|反复|出现|存在|情况|问题|排查|诊断|分析|"
    r"给出|提供|看看|检查|以及|并且|然后|现在|今天|昨天|一下|帮我|报告|步骤|根因|建议|的|了|吗|呢|啊)"
)


def _step(tool: str, args: dict[str, Any], reason: str, depends: list[str] | None = None) -> Step:
    return Step(id=new_id("step"), tool=tool, args=args, reason=reason, depends_on=depends or [])


def extract_slots(goal: str) -> dict[str, Any]:
    """从自然语言里抽主机/指标/窗口/关键词/URL。抽不到就留空，由模板决定兜底。"""
    slots: dict[str, Any] = {}
    low = goal.lower()
    for host in data_store.host_ids():
        if host.lower() in low:
            slots["host"] = host
            break
    else:
        # 用户报了个清单里不存在的主机名（ghost-09）：保留他说的字面量，
        # 让工具层的"不在巡检清单"错误信息能回给他一个可行动的原因，
        # 而不是在这里 KeyError 把整次运行崩掉。
        guess = re.search(r"\b([a-z][a-z0-9]*[-_]\d{1,3})\b", goal)
        if guess:
            slots["unknown_host"] = guess.group(1)
    if "host" not in slots:
        for host, info in data_store.hosts().get("hosts", {}).items():
            role = str(info.get("role", "")).lower()
            if role and role in low:
                slots["host"] = host
                break
    for alias, metric in METRIC_ALIASES.items():
        if alias in low:
            slots["metric"] = metric
            break
    for label, pattern, pref_metric in SYMPTOM_PATTERNS:
        if label.lower() in low:
            slots["symptom"] = label
            slots["symptom_pattern"] = pattern
            slots.setdefault("metric", pref_metric)
            break
    m = re.search(r"(?:近|最近|过去)\s*(\d+)\s*(小时|天|h|d)", goal)
    if m:
        n = int(m.group(1))
        slots["window_hours"] = n if m.group(2) in ("小时", "h") else n * 24
    url = re.search(r"https?://[^\s，。]+", goal)
    if url:
        slots["url"] = url.group(0)
    quoted = re.search(r"[「『\"“](.+?)[」』\"”]", goal)
    if quoted:
        slots["keyword"] = quoted.group(1).strip()
    else:
        stripped = _STOPWORDS.sub("", goal).strip()
        slots["keyword"] = (stripped or slots.get("symptom") or goal.strip())[:24]
    # 交付意图：目标里明确要报告/要工单时，计划才追加写操作（且默认会被护栏拦下要求授权）
    slots["want_report"] = any(k in goal for k in ("报告", "报表", "交付", "存档"))
    slots["want_ticket"] = any(k in goal for k in ("工单", "派单", "转人工", "跟进"))
    return slots


# --------------------------------------------------------------------------- 模板
def plan_inspect_host(slots: dict[str, Any]) -> list[Step]:
    # 未知主机名照样下发给工具：由工具层给出"不在巡检清单（可用：…）"的可行动原因，
    # 而不是规划器自己崩掉。这里曾经写 slots["host"] 直接 KeyError。
    host = str(slots.get("host") or slots.get("unknown_host") or "")
    if not host:
        return plan_explore(slots)
    win = int(slots.get("window_hours", 24))
    metric = slots.get("metric", "disk")
    steps = [
        _step("metrics_query", {"host": host, "window_hours": win, "metric": metric},
              f"先看 {host} 的 {metric} 水位，判断是不是资源侧问题"),
        _step("metrics_query", {"host": host, "window_hours": win, "metric": "cpu"},
              f"再看 {host} 的 CPU 水位"),
        _step("service_status", {"host": host}, f"确认 {host} 上各服务是否在线、有无频繁重启"),
        _step("config_check", {"host": host, "component": ""}, f"对 {host} 做配置体检，找可整改项"),
    ]
    if metric == "disk":
        steps.append(_step("disk_forecast", {"host": host, "horizon_days": 14},
                           f"预测 {host} 磁盘多久触及告警线", [steps[0].id]))
    rc = _step("root_cause_rank", {"host": host, "lookback_hours": win},
               "融合上述证据给出根因排序", [s.id for s in steps])
    steps.append(rc)
    # 只有目标明确要交付物时才追加写操作；参数由上游观察派生，不手写死值
    if slots.get("want_report"):
        steps.append(_step("report_render", {"host": host, "title": f"{host} 巡检报告", "fmt": "markdown"},
                           "把本次巡检落成交付文件（写操作，需授权）", [rc.id]))
    if slots.get("want_ticket"):
        steps.append(_step(
            "ticket_create",
            {"host": host, "severity": f"${rc.id}.severity",
             "summary": f"${rc.id}.summary", "evidence_refs": f"$evid:{rc.id}"},
            "按根因结论开处置工单（写操作，需授权，且必须带证据引用）", [rc.id]))
    return steps


def plan_diagnose(slots: dict[str, Any]) -> list[Step]:
    host = slots.get("host", "")
    win = int(slots.get("window_hours", 24))
    pattern = slots.get("symptom_pattern") or slots.get("keyword") or "error"
    topic = slots.get("symptom") or slots.get("keyword") or "异常"
    metric = slots.get("metric", "cpu")
    steps: list[Step] = []
    if host:
        steps.append(_step("metrics_query", {"host": host, "window_hours": win, "metric": metric},
                           f"取 {host} 的 {metric} 指标，先确认异常是否存在"))
    steps.append(_step("log_search", {"service": "", "pattern": pattern, "level": "ERROR", "limit": 5},
                       f"按症状模式「{pattern[:40]}」检索错误日志并聚合，识别刷屏的同类错误"))
    if host:
        prev = [s.id for s in steps]
        steps.append(_step("root_cause_rank", {"host": host, "lookback_hours": win},
                           f"把 {host} 的指标/日志/配置证据融合成根因排序", prev))
    probe = steps[-1] if host else steps[0]
    kb = _step("kb_search", {"query": f"{topic} 处置", "top_k": 3},
              f"在运维手册里找「{topic}」对应的处置方案")
    steps.append(kb)
    steps.append(_step("runbook_get", {"runbook_id": f"${probe.id}.top_runbook"},
                       "取回最匹配手册的完整处置步骤", [kb.id]))
    if host:
        steps.append(_step("incident_history", {"host": host, "keyword": topic},
                           f"查 {host} 的同类历史故障，拿 MTTR 基线并判断是否老毛病", [kb.id]))
    return steps


def plan_capacity(slots: dict[str, Any]) -> list[Step]:
    host = slots.get("host") or ""
    if not host:
        return [_step("kb_search", {"query": "容量 磁盘 清理 扩容", "top_k": 3},
                      "未指明主机，先检索容量管理规范")]
    return [
        _step("metrics_query", {"host": host, "window_hours": 168, "metric": "disk"},
              f"取 {host} 7 天磁盘水位作为外推样本"),
        _step("disk_forecast", {"host": host, "horizon_days": 14},
              f"外推 {host} 磁盘触及告警线的时间与置信度"),
        _step("config_check", {"host": host, "component": ""},
              f"检查 {host} 是否有 DEBUG 日志/未备份等加速磁盘消耗的配置", ),
        _step("kb_search", {"query": "磁盘 清理 日志轮转 扩容", "top_k": 3},
              "检索磁盘治理手册"),
    ]


def plan_log_triage(slots: dict[str, Any]) -> list[Step]:
    pattern = slots.get("symptom_pattern") or slots.get("keyword") or "error"
    topic = slots.get("symptom") or slots.get("keyword") or "错误"
    host = slots.get("host", "")
    logs = _step("log_search", {"service": "", "pattern": pattern, "level": "ERROR", "limit": 8},
                 f"检索匹配「{pattern[:40]}」的错误日志并按模板聚合")
    steps = [logs]
    if host:
        steps.append(_step("service_status", {"host": host}, f"确认 {host} 服务是否因此报错而离线", [logs.id]))
    steps.append(_step("incident_history", {"host": host, "keyword": topic}, "查历史同类故障与 MTTR"))
    steps.append(_step("kb_search", {"query": f"{topic} 处置", "top_k": 3}, "检索对应处置手册"))
    return steps


def plan_probe_service(slots: dict[str, Any]) -> list[Step]:
    url = slots.get("url", "")
    host = slots.get("host", "")
    steps: list[Step] = []
    if url:
        steps.append(_step("http_probe", {"url": url, "timeout_ms": 3000}, f"对 {url} 做真实探活"))
    if host:
        steps.append(_step("service_status", {"host": host}, f"查 {host} 上服务的端口与在线状态"))
        steps.append(_step("topology_neighbors", {"host": host}, f"查 {host} 上下游，判断影响面"))
    if not steps:
        steps.append(_step("kb_search", {"query": "健康检查 探活 规范", "top_k": 3},
                          "目标不明确，检索探活规范作为回答依据"))
    return steps


def plan_kb_lookup(slots: dict[str, Any]) -> list[Step]:
    kw = slots.get("keyword", "运维 规范")
    kb = _step("kb_search", {"query": kw, "top_k": 3}, f"检索手册中关于「{kw}」的条目")
    return [kb, _step("runbook_get", {"runbook_id": f"${kb.id}.refs.0.id"},
                      "取回最相关条目的完整处置步骤", [kb.id])]


def plan_explore(slots: dict[str, Any]) -> list[Step]:
    steps = [_step("kb_search", {"query": slots.get("keyword", ""), "top_k": 3}, "检索知识库寻找相关规范")]
    host = slots.get("host")
    if host:
        steps.append(_step("metrics_query", {"host": host, "window_hours": 24, "metric": slots.get("metric", "cpu")},
                           f"顺带取 {host} 的关键指标作为佐证"))
        steps.append(_step("config_check", {"host": host, "component": ""}, f"顺带对 {host} 做配置体检"))
    else:
        steps.append(_step("incident_history", {"host": "", "keyword": slots.get("keyword", "")},
                           "查历史故障记录寻找相似案例"))
    return steps


_TEMPLATES = {
    "inspect_host": plan_inspect_host,
    "diagnose": plan_diagnose,
    "capacity": plan_capacity,
    "log_triage": plan_log_triage,
    "probe_service": plan_probe_service,
    "kb_lookup": plan_kb_lookup,
    "explore": plan_explore,
}


# --------------------------------------------------------------------------- LLM
SYSTEM_PROMPT = (
    "你是运维巡检智能体的任务规划器。把目标拆成有序的工具调用计划，只输出 JSON。\n"
    "硬性要求：\n"
    "1) tool 必须来自给定工具清单，逐字一致；参数名也必须逐字一致；\n"
    "2) 只读工具优先，写操作（side_effect=true）除非目标明确要求，否则不要出现；\n"
    "3) 步骤数不超过 max_steps；依赖用 depends_on 给出前置步骤的下标（从 0 开始）；\n"
    "4) 参数里的 host 只能是给定主机清单中的值，拿不准就留空字符串；\n"
    '5) 输出格式：{"intent":"inspect_host|diagnose|capacity|log_triage|probe_service|kb_lookup|explore",'
    '"steps":[{"tool":"...","args":{...},"reason":"为什么需要这步","depends_on":[0]}]}'
)


def llm_plan(goal: str, registry: ToolRegistry, slots: dict[str, Any]) -> tuple[list[Step] | None, dict[str, Any]]:
    """向模型要一份计划，并逐条校验。返回 (步骤或 None, 埋点)。"""
    s = get_settings()
    client = LlmClient()
    manifest = json.dumps(registry.prompt_manifest(), ensure_ascii=False)
    user = (
        f"目标：{goal}\n已识别槽位：{json.dumps(slots, ensure_ascii=False)}\n"
        f"主机清单：{', '.join(data_store.host_ids())}\n"
        f"工具清单：{manifest}\n最多 {s.MAX_STEPS} 步。"
    )
    obj, meta = client.chat_json(SYSTEM_PROMPT, user, purpose="plan")
    if not isinstance(obj, dict):
        # 不能原样返回 meta：chat_json 成功时 outcome="ok"，
        # 那样 make_plan 会记成"因为 ok 所以回落到规则引擎"，把失败伪装成成功。
        return None, {**meta, "outcome": "invalid_payload"}
    raw_steps = obj.get("steps") or []
    if not isinstance(raw_steps, list) or not raw_steps:
        return None, {**meta, "outcome": "invalid_shape"}
    steps: list[Step] = []
    id_by_index: dict[int, str] = {}
    dropped: list[str] = []
    for i, item in enumerate(raw_steps[: s.MAX_STEPS]):
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool", ""))
        if not registry.has(tool):
            dropped.append(str(tool))
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        deps = [id_by_index[int(d)] for d in (item.get("depends_on") or [])
                if isinstance(d, (int, str)) and str(d).lstrip("-").isdigit() and int(d) in id_by_index]
        st = _step(tool, args, str(item.get("reason", ""))[:120] or "模型规划", deps)
        id_by_index[i] = st.id
        steps.append(st)
    if dropped:
        meta["dropped_unknown_tools"] = dropped
    if not steps:
        return None, {**meta, "outcome": "all_steps_invalid"}
    meta["intent_from_llm"] = str(obj.get("intent", ""))
    return steps, meta


def make_plan(
    goal: str, registry: ToolRegistry, mode: str | None = None
) -> tuple[list[Step], str, str, dict[str, Any], dict[str, Any]]:
    """入口。返回 (步骤, 引擎, 意图, 槽位, LLM 埋点)。

    engine ∈ {rule, llm, llm→rule}，评测分组与面板展示都要用它。
    """
    s = get_settings()
    mode = (mode or s.PLAN_MODE).lower()
    slots = extract_slots(goal)
    intent = detect_intent(goal)
    rule_steps = _TEMPLATES.get(intent, plan_explore)(slots)
    if mode == "rule" or not LlmClient().available():
        return rule_steps, "rule", intent, slots, {}
    steps, meta = llm_plan(goal, registry, slots)
    if steps:
        return steps, "llm", intent, slots, meta
    meta["fallback_reason"] = meta.get("reason") or meta.get("outcome") or "模型未给出可用计划"
    return rule_steps, "llm→rule", intent, slots, meta
