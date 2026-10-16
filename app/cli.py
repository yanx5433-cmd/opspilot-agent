"""命令行入口：跑巡检、看轨迹、查工具、跑评测。

CLI 是这个项目的主交付形态之一——无人值守场景就是 `cron` 调它，
所以它必须能纯文本输出、能用退出码表达结果、能不写任何文件（--no-trace）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Windows 控制台默认 GBK，中文输出会花屏；强制 UTF-8 让 CLI 在 cmd/PowerShell 下都可读。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from app import data_store

STATUS_EXIT = {"done": 0, "partial": 0, "blocked": 1, "failed": 2}


def _registry():
    from app.tools import build_registry

    return build_registry()


def cmd_run(args: argparse.Namespace) -> int:
    from app.agent.executor import OpsPilotAgent

    agent = OpsPilotAgent(_registry(), trace_dir=args.trace_dir, record_trace=not args.no_trace)
    approve = [a for a in (args.approve or "").split(",") if a]
    result = agent.run(args.goal, approve=approve or None, plan_mode=args.plan_mode)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(f"\n目标：{result.goal}")
        print(f"状态：{result.status}｜引擎：{result.mode}｜预算：{result.budget.snapshot()}")
        print("-" * 68)
        print(result.answer)
        if args.verbose:
            print("-" * 68)
            for st in result.steps:
                mark = {"done": "✓", "failed": "✗", "blocked": "⛔", "skipped": "→", "pending": " "}[st.status]
                print(f"{mark} {st.tool:<18} {st.reason[:40]}")
                if st.error:
                    print(f"  └ {st.error[:110]}")
    return STATUS_EXIT.get(result.status, 3)


def cmd_tools(_args: argparse.Namespace) -> int:
    r = _registry()
    print(f"共 {len(r.names())} 个工具｜只读 {len(r.read_tools())} 个｜写操作 {len(r.write_tools())} 个\n")
    for d in r.decls():
        tag = "写" if d.side_effect else "读"
        req = ",".join(d.required) or "-"
        print(f"[{tag}] {d.name:<20} 参数: {', '.join(f'{k}:{v}' for k, v in d.params.items())}")
        print(f"     必需: {req}｜{d.description[:88]}")
    return 0


def cmd_data(_args: argparse.Namespace) -> int:
    print(json.dumps(data_store.describe(), ensure_ascii=False, indent=2))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from app.agent.trace import replay

    path = args.task_id if args.task_id.endswith(".jsonl") else os.path.join(args.trace_dir, f"{args.task_id}.jsonl")
    if not os.path.exists(path):
        d = args.trace_dir
        avail = sorted(f for f in os.listdir(d) if f.endswith(".jsonl"))[-8:] if os.path.isdir(d) else []
        print(f"轨迹文件不存在：{path}\n最近可用：{', '.join(avail) or '（无）'}")
        return 2
    for line in replay(path):
        print(line)
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    from evals.runner import main as eval_main

    return eval_main(["--suite", args.suite, "--fail-under", str(args.fail_under)])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="opspilot", description="OpsPilot 巡检与故障诊断智能体")
    p.add_argument("--trace-dir", default=os.environ.get("OPSPILOT_TRACE_DIR", "run/traces"))
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="执行一次任务")
    r.add_argument("goal")
    r.add_argument("--approve", default="", help="逗号分隔的写工具授权清单，如 report_render,ticket_create")
    r.add_argument("--plan-mode", default=None, choices=["auto", "rule", "llm"])
    r.add_argument("--json", action="store_true", help="输出完整结果 JSON（含轨迹事件）")
    r.add_argument("--no-trace", action="store_true", help="不落轨迹文件")
    r.add_argument("-v", "--verbose", action="store_true", help="打印每一步状态")
    r.set_defaults(func=cmd_run)

    t = sub.add_parser("tools", help="列出工具与读写分级")
    t.set_defaults(func=cmd_tools)

    d = sub.add_parser("data", help="打印当前数据集规模")
    d.set_defaults(func=cmd_data)

    rp = sub.add_parser("replay", help="回放一条轨迹")
    rp.add_argument("task_id")
    rp.set_defaults(func=cmd_replay)

    ev = sub.add_parser("eval", help="运行离线评测集")
    ev.add_argument("--suite", default="all")
    ev.add_argument("--fail-under", type=float, default=0.0)
    ev.set_defaults(func=cmd_eval)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except FileNotFoundError as exc:
        print(f"数据缺失：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
