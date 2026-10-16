"""工具注册：把 14 个能力声明出来，注册时即校验"声明 = 实现"。

11 只读 + 3 写（写操作默认全部需要人工授权）。
工具名是给 LLM 看的、也是护栏查的、也是评测断言的——同一份声明三处复用。
"""
from __future__ import annotations

from ..agent.tools_registry import ToolRegistry
from . import kb, ops, write


def build_registry() -> ToolRegistry:
    r = ToolRegistry()

    r.register(
        _decl(
            name="metrics_query",
            description="查询指定主机的指标时序（cpu/mem/disk/io_wait/load/net_in_mbps），返回当前值、峰值、均值、趋势与 EWMA 异常点。",
            params={"host": "str", "window_hours": "int", "metric": "str"},
            required=["host", "window_hours", "metric"],
            side_effect=False, evidence_kind="metric",
            enum_params={"metric": list(ops.VALID_METRICS)},
            examples=[{"host": "web-01", "window_hours": 24, "metric": "disk"}],
        ), ops.metrics_query)

    r.register(
        _decl(
            name="log_search",
            description="按服务名、关键词（支持正则）、日志级别下限检索日志，并把同类错误按模板聚合计数。level=ERROR 会同时命中 FATAL。",
            params={"service": "str", "pattern": "str", "level": "str", "limit": "int"},
            required=["pattern", "level", "limit"],
            side_effect=False, evidence_kind="log",
            enum_params={"level": ["", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]},
            examples=[{"service": "app-order", "pattern": "timeout", "level": "ERROR", "limit": 5}],
        ), ops.log_search)

    r.register(
        _decl(name="service_status",
              description="查指定主机上各服务的端口、在线状态与 24 小时重启次数。",
              params={"host": "str"}, required=["host"],
              side_effect=False, evidence_kind="probe"),
        ops.service_status)

    r.register(
        _decl(name="config_check",
              description="对指定主机（可选单个组件）做配置体检，按 lint 规则输出可整改项与严重级。",
              params={"host": "str", "component": "str"}, required=["host"],
              side_effect=False, evidence_kind="config",
              examples=[{"host": "db-01", "component": ""}]),
        ops.config_check)

    r.register(
        _decl(name="disk_forecast",
              description="用线性回归外推磁盘水位触及告警线的天数，并给出 R² 与置信档；趋势不可信时拒答。",
              params={"host": "str", "horizon_days": "int"}, required=["host", "horizon_days"],
              side_effect=False, evidence_kind="metric"),
        ops.disk_forecast)

    r.register(
        _decl(name="http_probe",
              description="对 URL 做真实 HTTP 探活，返回状态码与耗时。目标须在主机清单或为本机回环（SSRF 防护）。",
              params={"url": "str", "timeout_ms": "int"}, required=["url", "timeout_ms"],
              side_effect=False, evidence_kind="probe"),
        ops.http_probe)

    r.register(
        _decl(name="incident_history",
              description="查同类历史故障，返回平均修复时长 MTTR 与反复发生的根因清单。",
              params={"host": "str", "keyword": "str"}, required=["keyword"],
              side_effect=False, evidence_kind="doc"),
        ops.incident_history)

    r.register(
        _decl(name="topology_neighbors",
              description="查指定主机的上下游依赖，用于判断故障是自身引起还是依赖引起。",
              params={"host": "str"}, required=["host"],
              side_effect=False, evidence_kind="doc"),
        ops.topology_neighbors)

    r.register(
        _decl(name="root_cause_rank",
              description="融合指标/日志/配置三类证据，输出根因假设排序与概率，并给出严重级与对应手册。",
              params={"host": "str", "lookback_hours": "int"}, required=["host", "lookback_hours"],
              side_effect=False, evidence_kind="doc"),
        ops.root_cause_rank)

    r.register(
        _decl(name="kb_search",
              description="在运维手册（Runbook）知识库中检索处置方案，返回标题、来源文件、相关度与步骤条数。",
              params={"query": "str", "top_k": "int"}, required=["query"],
              side_effect=False, evidence_kind="doc"),
        kb.kb_search)

    r.register(
        _decl(name="runbook_get",
              description="按手册 id 取回完整处置步骤（先 kb_search 拿到 id 再取全文）。",
              params={"runbook_id": "str"}, required=["runbook_id"],
              side_effect=False, evidence_kind="doc"),
        kb.runbook_get)

    r.register(
        _decl(name="ticket_create",
              description="【写】创建处置工单并登记台账。必须携带证据引用，无引用的工单直接拒绝。",
              params={"host": "str", "severity": "str", "summary": "str", "evidence_refs": "list"},
              required=["host", "severity", "summary", "evidence_refs"],
              side_effect=True, evidence_kind="ticket",
              enum_params={"severity": ["P0", "P1", "P2", "P3"]}),
        write.ticket_create)

    r.register(
        _decl(name="notify_send",
              description="【写】生成告警通知。默认 dry_run=true 只落台账、不真实投递。",
              params={"channel": "str", "message": "str", "dry_run": "bool"},
              required=["channel", "message", "dry_run"],
              side_effect=True, evidence_kind="ticket",
              enum_params={"channel": ["wecom", "dingtalk", "email", "console"]}),
        write.notify_send)

    r.register(
        _decl(name="report_render",
              description="【写】生成单机巡检报告文件（指标水位/容量预测/根因分析/配置体检四章）。",
              params={"host": "str", "title": "str", "fmt": "str"},
              required=["host", "fmt"],
              side_effect=True, evidence_kind="report",
              enum_params={"fmt": ["markdown", "text"]}),
        write.report_render)

    return r


def _decl(**kwargs):
    from ..agent.tools_registry import ToolDecl

    return ToolDecl(**kwargs)
