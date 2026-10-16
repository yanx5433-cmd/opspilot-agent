"""写操作工具：Agent 唯一能改变外部世界的三只手，全部默认需要人工授权。

台账而不是静默：每一次写都 append 一行 JSONL（谁、为什么、依据哪几条证据），
可复盘、可审计。notify_send 默认 dry-run —— 真实项目里"Agent 自动发通知"
是最容易翻车的能力，宁可只做草拟。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Any

from .. import data_store
from ..config import get_settings
from .ops import config_check, disk_forecast, metrics_query, result, root_cause_rank


def _append_ledger(name: str, record: dict[str, Any]) -> str:
    d = get_settings().ARTIFACT_DIR
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _stamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def ticket_create(host: str, severity: str, summary: str, evidence_refs: list) -> dict[str, Any]:
    """创建处置工单。evidence_refs 必须逐条对应真实观察，写进台账供复核。"""
    if severity not in {"P0", "P1", "P2", "P3"}:
        return result(f"非法严重级 {severity!r}（P0~P3）", "ticket", f"ticket:{host}",
                      ok=False, error="bad_severity")
    refs = [str(r) for r in (evidence_refs or []) if str(r).strip()]
    if not refs:
        return result("工单被拒绝：没有证据引用的工单等同于凭空报障",
                      "ticket", f"ticket:{host}", ok=False, error="no_evidence")
    # 稳定 ID：内置 hash() 按进程加盐，同一工单两次运行会拿到不同编号，
    # 台账就没法去重了。这里必须用内容哈希。
    digest = hashlib.sha1(f"{host}|{severity}|{summary}".encode()).hexdigest()[:6].upper()
    tid = f"T{dt.datetime.now().strftime('%Y%m%d')}-{digest}"
    record = {"ticket_id": tid, "ts": _stamp(), "host": host, "severity": severity,
              "summary": summary, "evidence_refs": refs, "source": "opspilot-agent"}
    path = _append_ledger("tickets.jsonl", record)
    return result(f"工单 {tid}（{severity}）已登记于 {path}，附 {len(refs)} 条证据引用",
                  "ticket", f"ticket:{tid}", ticket_id=tid, path=path,
                  evidence_count=len(refs), record=record)


def notify_send(channel: str, message: str, dry_run: bool) -> dict[str, Any]:
    """生成告警草案。真实投递通道**没有实现**，所以 dry_run=false 一律拒绝。

    这里刻意不写"已按配置投递"：台账里 delivered 恒为 False，
    而结论层告诉值班的人"通知已经发出去了"是最恶劣的一类幻觉。
    """
    if channel not in {"wecom", "dingtalk", "email", "console"}:
        return result(f"未支持的通道 {channel!r}（可选 wecom/dingtalk/email/console）",
                      "ticket", f"notify:{channel}", ok=False, error="bad_channel")
    if not dry_run:
        return result(
            "真实投递未实现：本工具只做告警草拟，不会把消息送进任何 IM/邮件系统。"
            "需要真实投递请改用 dry_run=true 拿草案，或自行接入 webhook 后再放开",
            "ticket", f"notify:{channel}", ok=False, error="delivery_not_implemented",
            channel=channel, message=message)
    record = {"ts": _stamp(), "channel": channel, "message": message,
              "dry_run": True, "delivered": False}
    path = _append_ledger("notify_ledger.jsonl", record)
    return result(f"告警通知（{channel}）草案已落台账，未真实投递（dry-run）", "ticket",
                  f"notify:{channel}", path=path, dry_run=True, message=message)


def report_render(host: str, title: str, fmt: str) -> dict[str, Any]:
    """把单机巡检结果渲染成可交付文件（Markdown / 纯文本）。

    报告内容全部来自工具观察，不掺模型生成——无人值守场景里，
    报表数字被模型"润色"过一次就等于不可信了。
    """
    if fmt not in {"markdown", "text"}:
        return result(f"不支持的格式 {fmt!r}（markdown/text）", "report", f"report:{host}",
                      ok=False, error="bad_format")
    ext = "md" if fmt == "markdown" else "txt"
    fname = f"{host}-inspection-{dt.datetime.now().strftime('%Y%m%d')}.{ext}"
    would_be = os.path.join(get_settings().ARTIFACT_DIR, fname)
    if host not in data_store.host_ids():
        # 一次成功观察都没有的"交付物"必须是拒绝，而不是写出一份四章全空的报告
        return result(f"拒绝生成报告：主机 {host!r} 不在巡检清单，无任何可写入报告的观察",
                      "report", f"report:{host}", ok=False, error="unknown_host",
                      path=would_be, written=False)
    metrics = [metrics_query(host, 24, m) for m in ("cpu", "mem", "disk")]
    cfg = config_check(host, "")
    fc = disk_forecast(host, 14)
    rc = root_cause_rank(host, 24)
    info = data_store.hosts().get("hosts", {}).get(host, {})
    lines = [
        f"# {title or f'{host} 巡检报告'}",
        "",
        f"- 生成时间：{_stamp()}",
        f"- 主机：{host}｜角色 {info.get('role', '-')}｜环境 {info.get('env', '-')}｜责任人 {info.get('owner', '-')}",
        "",
        "## 一、指标水位",
        "",
        "| 指标 | 当前 | 峰值 | 均值 | 趋势 | 告警线 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for m in metrics:
        if not m["ok"]:
            continue
        lines.append(f"| {m['metric']} | {m['latest']}% | {m['peak']}% | {m['mean']}% | "
                     f"{m['trend']} | {str(m['threshold']) + '%' if m.get('threshold') else '-'} |")
    lines += ["", "## 二、容量预测", ""]
    lines.append(f"- {fc['summary']}" if fc["ok"] else f"- 未能给出容量预测：{fc['summary']}")
    lines += ["", "## 三、根因分析", ""]
    if rc["ok"] and rc["hypotheses"]:
        for i, h in enumerate(rc["hypotheses"], 1):
            lines.append(f"{i}. {h['cause']} — 概率 {h['probability']:.0%}（依据证据类型 {h['supporting_evidence']}）"
                         + (f"；对应手册 {h['runbook']}" if h.get("runbook") else ""))
    else:
        lines.append(f"- {rc['summary']}")
    lines += ["", "## 四、配置体检", ""]
    if cfg["ok"]:
        if cfg["findings"]:
            lines.append("| 组件 | 配置项 | 当前值 | 级别 | 整改建议 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for f in cfg["findings"]:
                lines.append(f"| {f['component']} | {f['key']} | {f['value']} | {f['severity']} | {f['advice']} |")
        else:
            lines.append("- 未发现待整改项")
    lines += ["", "---", "", "本报告由 OpsPilot 巡检智能体生成；数据为确定性演示数据，非真实生产数据。", ""]
    content = "\n".join(lines)
    d = get_settings().ARTIFACT_DIR
    os.makedirs(d, exist_ok=True)
    # 文件名按"主机+日期"收敛：同日重跑覆盖同一份报告，而不是每次留一个新文件。
    # 无人值守场景里，跑一个月就攒出 30 个近似文件是种污染。
    path = would_be
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return result(f"巡检报告已写入 {path}（{len(content)} 字符，含 4 个章节）", "report",
                  f"report:{path}", path=path, bytes=len(content), host=host,
                  sections=["指标水位", "容量预测", "根因分析", "配置体检"])
