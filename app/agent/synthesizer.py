"""综合层：把证据写成结论，并且**让每句话都必须能追溯**。

两种模式共用同一道引用闸门：
- 规则模式：模板生成，天生带引用（用于评测基线、无 Key 场景）；
- LLM 模式：模型撰写，但输出必须过 verify()——没有有效 [ev:id] 的事实句直接裁掉。

所以"幻觉"在这里不是一个形容词，而是一个可数的指标：dropped_claims 条数、
unevidenced_ratio 比例、numeric_suspects 数字对不上的句子。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .evidence import EvidenceLedger
from .llm import LlmClient
from .models import Budget

CITATION_RE = re.compile(r"\[ev:([A-Za-z0-9_]+)\]")
_NUM_RE = re.compile(r"(?<![\w.\-])\d+(?:\.\d+)?%?(?![\w.\-])")
_HEADING = re.compile(r"^(#{1,6}\s|【|[-*]\s{0,2}$)")
_STRUCTURAL = re.compile(r"^(结论|关键发现|处置建议|未覆盖|阻塞|依据|说明|风险提示)")
# 这些小节描述的是"没发生什么"，不要求引用（详见 verify 的说明）
_EXEMPT_SECTION = re.compile(r"(阻塞|未覆盖|说明|附录)")


@dataclass
class VerifyResult:
    clean: str
    kept: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    invalid_refs: list[str] = field(default_factory=list)
    numeric_suspects: list[str] = field(default_factory=list)

    @property
    def unevidenced_ratio(self) -> float:
        factual = len(self.kept) + len(self.dropped)
        return round(len(self.dropped) / factual, 3) if factual else 0.0


def _is_factual(line: str) -> bool:
    """标题、空行、章节名不算"论断"，不要求引用；带数字或因果词的句子才算。"""
    text = line.strip()
    if not text or _HEADING.match(text) or _STRUCTURAL.match(text):
        return False
    return not text.startswith(("预算", "模式", "生成", "数据说明", "引用"))


def _collect_numbers(obj: Any, out: set[str]) -> None:
    """递归收集证据里"可被合法引用"的数字写法。

    一个数在证据里可能是 0.26 / "26%" / 列表长度 / 字符串里的 id，
    只比对字面量会把合法结论误判成幻觉；这里把常见等价写法都展开。
    """
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(str(obj))
        out.add(str(round(obj, 1)))
        out.add(str(round(obj)))
        out.add(str(round(obj * 100, 1)))
        out.add(str(round(obj * 100)))   # round(float) 已是 int，再套 int() 是多余的
        text = f"{obj}"
        out.add(text.rstrip("0").rstrip(".") or "0")
    elif isinstance(obj, str):
        for m in re.findall(r"\d+(?:\.\d+)?", obj):
            out.add(m)
            val = float(m)
            if val.is_integer():
                out.add(str(int(val)))
    elif isinstance(obj, dict):
        out.add(str(len(obj)))
        for value in obj.values():
            _collect_numbers(value, out)
    elif isinstance(obj, (list, tuple)):
        out.add(str(len(obj)))
        for value in obj:
            _collect_numbers(value, out)


# 运行元信息的专用标记：这类句子描述"本次运行发生了什么"，不是对世界的论断，
# 因此不该被要求出示证据。用哨兵字符串而不是豁免整个小节，
# 是为了不给模型留一个"把幻觉写进 ## 结论 下面"的通道。
_META = "\x00meta\x00"


def meta_line(text: str) -> str:
    """把一句话标记为运行元信息（verify 会剥掉标记并放行）。"""
    return _META + text


def verify(text: str, ledger: EvidenceLedger) -> VerifyResult:
    """引用闸门：句子必须引用存在的证据，否则裁掉。

    例外：`## 阻塞与未覆盖` 这类小节记录的是"没有发生什么"，天生无证据可引。
    若在这里一刀切，输出会留下一个空标题，让读的人以为一切正常——
    掩盖失败比丢失措辞严重得多，所以按小节粒度豁免。
    """
    valid = set(ledger.ids())
    numbers: dict[str, set[str]] = {}
    for e in ledger.items:
        bag: set[str] = set()
        _collect_numbers(e.data, bag)
        _collect_numbers(e.summary, bag)
        numbers[e.id] = bag
    res = VerifyResult(clean="")
    kept_lines: list[str] = []
    exempt_section = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if _HEADING.match(line.strip()):
            exempt_section = bool(_EXEMPT_SECTION.search(line))
        if not line.strip():
            kept_lines.append(line)
            continue
        if _META in line:
            stripped = line.replace(_META, "")
            kept_lines.append(stripped)
            res.kept.append(stripped.strip())
            continue
        refs = CITATION_RE.findall(line)
        bad = [r for r in refs if r not in valid]
        if bad:
            res.invalid_refs.extend(bad)
            line = CITATION_RE.sub(lambda m: m.group(0) if m.group(1) in valid else "", line)
            refs = [r for r in refs if r in valid]
        if exempt_section or not _is_factual(line):
            kept_lines.append(line)
            res.kept.append(line.strip())
            continue
        if not refs:
            res.dropped.append(line.strip())
            continue
        kept_lines.append(line)
        res.kept.append(line.strip())
        # "步骤 1：" 这类序号不是测量值，不能当数字去溯源
        allowed = set().union(*(numbers.get(r, set()) for r in refs)) if refs else set()
        scan = re.sub(r"(?:步骤|第)\s*\d+[：:.]?", "", CITATION_RE.sub("", line))
        for token in _NUM_RE.findall(scan):
            needle = token.rstrip("%")
            if needle and needle not in allowed:
                res.numeric_suspects.append(f"{token} <- {line.strip()[:60]}")
                break
    res.clean = "\n".join(kept_lines).strip()
    return res


def rule_answer(goal: str, intent: str, ledger: EvidenceLedger, blockers: list[str],
                budget: Budget, mode: str) -> str:
    """规则模式：按证据种类分节拼装，每行天生带引用。"""
    if not ledger.items:
        blocked = [meta_line(f"- 本次运行被拦下的步骤：{b}") for b in blockers] or [
            meta_line("- 无")]
        return "\n".join([
            "## 结论",
            meta_line("- 本次未能取得任何有效证据，不给结论（拒绝无依据作答）。"),
            "## 阻塞项",
            *blocked,
        ])
    lines = ["## 关键发现"]
    order = ["metric", "log", "probe", "config", "doc", "ticket", "report"]
    for kind in order:
        for ev in ledger.by_kind(kind):
            lines.append(f"- {ev.summary} `[ev:{ev.id}]`")
    advice = _advice(ledger, intent)
    if advice:
        lines.append("")
        lines.append("## 处置建议")
        lines.extend(advice)
    if blockers:
        lines.append("")
        lines.append("## 阻塞与未覆盖")
        lines.extend(f"- {b}" for b in blockers)
    lines.append("")
    lines.append(f"预算消耗：{budget.snapshot()}｜答复模式：{mode}")
    lines.append("数据说明：本结论基于确定性演示数据集（scripts/seed_demo.py），非真实生产数据。")
    return "\n".join(lines)


def _advice(ledger: EvidenceLedger, intent: str) -> list[str]:
    out: list[str] = []
    rc = next((e for e in ledger.by_kind("doc") if e.tool == "root_cause_rank"), None)
    if rc and rc.data.get("hypotheses"):
        h = rc.data["hypotheses"][0]
        rb = f"，对应手册 `{h['runbook']}`" if h.get("runbook") else ""
        out.append(f"- 优先按「{h['cause']}」处置（概率 {h['probability']:.0%}，"
                   f"严重级 {rc.data.get('severity', 'P-')}）{rb} `[ev:{rc.id}]`")
    steps_ev = next((e for e in ledger.by_kind("doc") if e.tool == "runbook_get"), None)
    if steps_ev and steps_ev.data.get("steps"):
        for i, s in enumerate(steps_ev.data["steps"][:4], 1):
            out.append(f"- 步骤 {i}：{s} `[ev:{steps_ev.id}]`")
    fc = next((e for e in ledger.by_kind("metric") if e.tool == "disk_forecast"), None)
    if fc and fc.data.get("forecast", {}).get("days_to_threshold") is not None:
        d = fc.data["forecast"]["days_to_threshold"]
        if d > 0:
            out.append(f"- 容量窗口：约 {int(d)} 天，需在窗口内完成清理或扩容 `[ev:{fc.id}]`")
        else:
            out.append("- 容量窗口：已越过告警线，按故障处置而不是按容量规划处理 " f"`[ev:{fc.id}]`")
    cfg = next((e for e in ledger.by_kind("config") if e.tool == "config_check"), None)
    if cfg and cfg.data.get("findings"):
        p1 = [f for f in cfg.data["findings"] if f["severity"] == "P1"]
        pick = (p1 or cfg.data["findings"])[:2]
        for f in pick:
            out.append(f"- 配置整改（{f['severity']}）：{f['component']}.{f['key']}={f['value']} → {f['advice']} `[ev:{cfg.id}]`")
    hist = next((e for e in ledger.by_kind("doc") if e.tool == "incident_history"), None)
    if hist and hist.data.get("avg_mttr_min"):
        out.append(f"- 历史同类故障平均修复 {hist.data['avg_mttr_min']} 分钟，可作为本次响应目标 `[ev:{hist.id}]`")
    if intent == "inspect_host" and len(ledger.items) >= 3 and not any(
        e.tool in ("report_render", "ticket_create") for e in ledger.items
    ):
        anchor = ledger.items[0].id
        out.append(f"- 由以上观察可派生交付物：建议落一份报告（report_render）"
                   f"或开一张工单（ticket_create），两者均需人工授权 `[ev:{anchor}]`")
    return out


def llm_answer(goal: str, intent: str, ledger: EvidenceLedger) -> tuple[str | None, dict[str, Any]]:
    """LLM 撰写：只给证据、只准引用证据。返回 (文本或 None, 埋点)。"""
    client = LlmClient()
    system = (
        "你是运维巡检智能体的结论撰写模块。严格遵守：\n"
        "1) 只依据用户提供的证据 JSON 作答，禁止引入证据之外的任何数字、主机名或结论；\n"
        "2) 每一个事实句末尾必须紧跟 `[ev:证据id]`，可引用多个；无法引用的句子不要写；\n"
        "3) 证据不足时用「无法判断」明说，不做乐观推断；\n"
        "4) 输出 Markdown，结构为：## 关键发现 / ## 处置建议，总长度 6~14 行，中文。\n"
        "你会被程序校验：没有有效 [ev:id] 的句子会被直接删除。"
    )
    user = (
        f"任务目标：{goal}\n意图：{intent}\n证据清单（每行一个 JSON）：\n{ledger.to_prompt()}\n"
        "现在写结论。"
    )
    obj, meta = client.chat_json(system, user, purpose=f"answer:{intent}", temperature=0.1)
    if isinstance(obj, dict):
        text = obj.get("answer") or obj.get("text") or obj.get("markdown")
        return (str(text) if text else None), meta
    if isinstance(obj, str):
        return obj, meta
    return None, meta
