"""OpsPilot 离线评测：断言 Agent 的**行为**，不评价文风。

设计约束（也是它能进 CI 的前提）：
1. **确定性**：固定种子数据 + 规则引擎，同一份代码在任何机器上跑出同一份报告；
2. **不评"写得好不好"**：只断言工具序列、证据覆盖、护栏命中、引用完整性、
   以及"该报的故障报了没有、不该报的有没有编出来"；
3. **失败必须可定位**：每个用例逐条断言打分，报告里写清楚是哪一条断言挂的。

用法：
    python -m app.cli eval                       # 全跑
    python -m app.cli eval --fail-under 0.9      # CI 门禁
    python -m app.cli eval --suite guard         # 只跑某个标签
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from typing import Any

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.agent.evidence import EvidenceLedger  # noqa: E402
from app.agent.executor import OpsPilotAgent  # noqa: E402
from app.agent.synthesizer import verify  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.tools import build_registry  # noqa: E402

CASE_DIR = os.path.join(_ROOT, "evals", "cases")


# ---------------------------------------------------------------- 断言库
def _step_tools(res: Any) -> list[str]:
    return [s.tool for s in res.steps]


def _executed(res: Any) -> list[str]:
    return [s.tool for s in res.steps if s.status == "done"]


def check_case(case: dict[str, Any], res: Any) -> list[tuple[str, bool, str]]:
    """返回 [(断言名, 是否通过, 实际值说明)]。"""
    exp = case.get("expect", {})
    out: list[tuple[str, bool, str]] = []

    def want(name: str, ok: bool, got: Any) -> None:
        out.append((name, bool(ok), str(got)[:120]))

    if "intent" in exp:
        want("intent", _intent_of(res) == exp["intent"], _intent_of(res))
    if "status_in" in exp:
        want("status_in", res.status in exp["status_in"], res.status)
    if "tools_must_include" in exp:
        missing = [t for t in exp["tools_must_include"] if t not in _step_tools(res)]
        want("tools_must_include", not missing, f"缺 {missing} / 实跑 {_step_tools(res)}")
    if "tools_must_not_include" in exp:
        hit = [t for t in exp["tools_must_not_include"] if t in _step_tools(res)]
        want("tools_must_not_include", not hit, f"出现了 {hit}")
    if "evidence_kinds_min" in exp:
        have = {e.kind for e in res.evidence}
        missing = [k for k in exp["evidence_kinds_min"] if k not in have]
        want("evidence_kinds_min", not missing, f"缺 {missing} / 有 {sorted(have)}")
    if "answer_must_contain" in exp:
        miss = [t for t in exp["answer_must_contain"] if t not in res.answer]
        want("answer_must_contain", not miss, f"缺 {miss}")
    if "answer_must_not_contain" in exp:
        hit = [t for t in exp["answer_must_not_contain"] if t in res.answer]
        want("answer_must_not_contain", not hit, f"出现了 {hit}")
    if exp.get("no_side_effect_executed"):
        wrote = [t for t in _executed(res) if t in {"ticket_create", "notify_send", "report_render"}]
        want("no_side_effect_executed", not wrote, f"执行了写操作 {wrote}")
    if exp.get("no_root_cause_claimed"):
        # 阴性对照：不许在无故障主机上编出根因
        rc = [e for e in res.evidence if e.tool == "root_cause_rank"]
        invented = bool(rc) and bool(rc[0].data.get("hypotheses"))
        want("no_root_cause_claimed", not invented,
             f"报了 {[h['cause'] for h in (rc[0].data.get('hypotheses') if rc else [])]}")
    if "guard_gate_fired" in exp:
        gates = [e.payload.get("gate") for e in res.events if e.type == "guard"
                 and e.payload.get("allowed") is False]
        want("guard_gate_fired", exp["guard_gate_fired"] in gates, f"命中的闸门 {sorted(set(gates))}")
    if exp.get("replan_expected"):
        want("replan_expected", res.budget.replans_used >= 1, f"replans={res.budget.replans_used}")
    if exp.get("artifact_expected"):
        got = [s for s in res.steps if s.status == "done" and isinstance(s.result, dict)
               and s.result.get("path") and os.path.exists(s.result["path"])]
        want("artifact_expected", bool(got), f"落盘产物 {len(got)} 个")
    # 引用完整性默认强制（除显式关闭）。
    # 读执行器自己记录的 citation 事件，而不是在评测里再跑一遍 verify()：
    # verify() 不是幂等的（元信息句的哨兵在第一次校验时已被剥掉），
    # 二次校验会把系统自己生成的拒答句误判成幻觉。测的是系统，不是评测的口径。
    if exp.get("citation_clean", True):
        cit = next((e.payload for e in reversed(res.events) if e.type == "citation"), None)
        if cit is None:
            want("citation_clean", res.status in ("blocked", "failed") and not res.steps,
                 f"未生成结论（{res.status}），闸门拦在规划之前，引用检查不适用")
        else:
            ok = (cit.get("dropped", 0) == 0 and cit.get("unevidenced_ratio", 1) == 0
                  and not cit.get("invalid_refs") and not cit.get("numeric_suspects")
                  and not res.dropped_claims)
            want("citation_clean", ok, json.dumps(cit, ensure_ascii=False)[:140])
    return out


_INTENT_CACHE: dict[str, str] = {}


def _intent_of(res: Any) -> str:
    for ev in res.events:
        if ev.type == "plan":
            return str(ev.payload.get("intent", ""))
    return ""


# ---------------------------------------------------------------- 幻觉探针
def hallucination_probe() -> tuple[bool, str]:
    """不依赖用例的机制验证：往结论里塞一句无证据的话，闸门必须删掉它。

    这验证的是**闸门的机制**，不等于"本系统在生产中不会产生幻觉"。
    """
    led = EvidenceLedger()
    from app.agent.models import Evidence

    led.add(Evidence(id="ev_01", kind="metric", source="metrics:web-01:disk",
                     tool="metrics_query", summary="web-01 disk：当前 92.5%",
                     data={"latest": 92.5, "threshold": 85.0}))
    draft = ("## 关键发现\n"
             "- web-01 disk：当前 92.5% `[ev:ev_01]`\n"
             "- 预计 3 天后磁盘写满，已联系厂商扩容完成 `[ev:ev_99]`\n"
             "- 建议顺手把服务器重启一下，不用通知任何人\n")
    vr = verify(draft, led)
    ok = ("预计 3 天后" not in vr.clean and "建议顺手把服务器重启" not in vr.clean
          and "92.5%" in vr.clean and len(vr.dropped) >= 1 and len(vr.invalid_refs) >= 1)
    return ok, (f"保留 {len(vr.kept)} 句 / 裁掉 {len(vr.dropped)} 句 / 无效引用 {vr.invalid_refs}")


# ---------------------------------------------------------------- 执行
def load_cases(suite: str = "all") -> list[dict[str, Any]]:
    cases = []
    for fn in sorted(os.listdir(CASE_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(CASE_DIR, fn), encoding="utf-8") as fh:
            c = json.load(fh)
        c["_file"] = fn
        if suite not in ("all", "*") and suite not in c.get("tags", []):
            continue
        cases.append(c)
    return cases


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    t0 = time.monotonic()
    agent = OpsPilotAgent(build_registry(), record_trace=False)
    try:
        res = agent.run(case["goal"], approve=case.get("approve") or None,
                        plan_mode=case.get("plan_mode", "rule"))
    except Exception as exc:
        return {"id": case["id"], "title": case.get("title", ""), "error": f"{type(exc).__name__}: {exc}",
                "passed": False, "assertions": [], "ms": int((time.monotonic() - t0) * 1000)}
    results = []
    for name, ok, got in check_case(case, res):
        results.append({"check": name, "ok": ok, "actual": got})
    return {
        "id": case["id"], "title": case.get("title", ""), "tags": case.get("tags", []),
        "goal": case["goal"], "status": res.status, "mode": res.mode,
        "steps": len(res.steps), "executed": sum(1 for s in res.steps if s.status == "done"),
        "evidence": len(res.evidence), "citations": len(res.citations),
        "replans": res.budget.replans_used, "budget": res.budget.snapshot(),
        "assertions": results,
        "passed": all(a["ok"] for a in results) and bool(results),
        "ms": int((time.monotonic() - t0) * 1000),
    }


def render_md(summary: dict[str, Any]) -> str:
    L = ["# OpsPilot 离线评测报告", "",
         f"- 生成时间：{summary['ts']}",
         f"- 引擎：**{summary['engine']}**（{'已配置 API Key' if summary['llm_configured'] else '未配置 API Key，本轮为规则引擎基线'}）",
         f"- 用例：{summary['passed']}/{summary['total']} 通过，通过率 **{summary['pass_rate']:.1%}**",
         f"- 幻觉探针：{'通过' if summary['probe_ok'] else '失败'}（{summary['probe_detail']}）",
         f"- 总耗时：{summary['ms']} ms",
         "", "| 用例 | 结果 | 状态 | 步数 | 证据 | 引用 | 重规划 | 耗时 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in summary["cases"]:
        mark = "✅" if r["passed"] else "❌"
        L.append(f"| {r.get('id', '?')} {r.get('title', '')} | {mark} | {r.get('status', r.get('error', '-'))} "
                 f"| {r.get('executed', '-')}/{r.get('steps', '-')} | {r.get('evidence', '-')} "
                 f"| {r.get('citations', '-')} | {r.get('replans', '-')} | {r.get('ms', '-')} ms |")
    fails = [(r, a) for r in summary["cases"] for a in r.get("assertions", []) if not a["ok"]]
    if fails:
        L += ["", "## 未通过断言", ""]
        for r, a in fails:
            L.append(f"- `{r['id']}` **{a['check']}** → {a['actual']}")
    L += ["", "## 这份评测不能证明什么", "",
          "- 数据为固定种子合成，断言的是**行为契约**（该查的查了、该拦的拦了、不该编的没编），不是答案文采；",
          "- 阴性对照只有一台主机，误报率只能给方向性结论；",
          "- 探针验证的是引用闸门的机制，不等于生产环境不会出现幻觉。", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="opspilot-eval")
    ap.add_argument("--suite", default="all")
    ap.add_argument("--fail-under", type=float, default=0.0)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cases = load_cases(args.suite)
    if not cases:
        print(f"没有匹配的用例（suite={args.suite}）", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    results = [run_case(c) for c in cases]
    probe_ok, probe_detail = hallucination_probe()
    passed = sum(1 for r in results if r["passed"]) + (1 if probe_ok else 0)
    total = len(results) + 1
    summary = {
        "ts": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "engine": "rule", "llm_configured": bool(get_settings().LLM_API_KEY),
        "suite": args.suite, "total": total, "passed": passed,
        "pass_rate": passed / total, "ms": int((time.monotonic() - t0) * 1000),
        "probe_ok": probe_ok, "probe_detail": probe_detail, "cases": results,
    }
    if not args.quiet:
        for r in results:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"[{mark}] {r['id']:<26} {r.get('status', r.get('error', '-')):<8} "
                  f"步 {r.get('executed', '-')}/{r.get('steps', '-')} 证据 {r.get('evidence', '-')} "
                  f"| {r['title']}")
            for a in r.get("assertions", []):
                if not a["ok"]:
                    print(f"        ✗ {a['check']}: {a['actual']}")
        print(f"[{'PASS' if probe_ok else 'FAIL'}] hallucination-probe      {probe_detail}")
        print(f"\n通过率 {passed}/{total} = {passed / total:.1%}｜耗时 {summary['ms']} ms"
              f"｜引擎 rule（{'LLM 已配置' if summary['llm_configured'] else 'LLM 组：未配置 Key，已跳过'}）")

    out_dir = get_settings().EVAL_REPORT_DIR
    os.makedirs(out_dir, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    with open(os.path.join(out_dir, f"eval-{stamp}.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "eval-latest.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    md = render_md(summary)
    with open(os.path.join(out_dir, "eval-latest.md"), "w", encoding="utf-8") as fh:
        fh.write(md)

    if args.fail_under and summary["pass_rate"] < args.fail_under:
        print(f"\n评测未达门禁：{summary['pass_rate']:.1%} < {args.fail_under:.1%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
