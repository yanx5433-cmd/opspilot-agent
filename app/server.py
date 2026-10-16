"""观测面板后端：把 Agent 的一次运行完整暴露出来（结论/轨迹/证据/护栏/预算）。

设计立场：
1. **只做只读观测 + 一次受控执行**。本文件不含任何业务决策——规划、护栏、
   引用校验全部复用 app.agent.*，面板看到的就等于 CLI 跑出来的，不存在"两套逻辑"。
2. **写权限必须由请求显式带来**。/api/run 的 approve 列表原样透传给 Agent，
   缺省即空列表（= 不授权任何写操作），绝不因为"这是 Web 面板"就放宽护栏。
3. **task_id 严格白名单**。轨迹文件名由外部输入拼路径，必须先过 ^task_[0-9a-f]{10}$，
   否则就是教科书级路径穿越。
4. **单实例串行执行**。executor 会临时改写进程内设置（APPROVED_WRITE_TOOLS），
   并发跑两个任务会互相污染授权清单，所以用一把锁把 /api/run 串起来。

默认只绑 127.0.0.1：这是本地调试面板，不是公网服务。需要局域网访问时显式
`--host 0.0.0.0`，届时请自行加鉴权（本项目故意不带凭据，见 README 安全说明）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK：不重配 UTF-8 的话，中文日志与 curl 输出在这台机器上会花屏。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app import (
    __version__,
    data_store,
)
from app.agent.executor import OpsPilotAgent
from app.agent.trace import read_trace
from app.config import get_settings
from app.tools import build_registry

# 轨迹文件名 = task_id：只允许这个形状，任何其他输入一律 400。
TASK_ID_RE = re.compile(r"^task_[0-9a-f]{10}$")
ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
EVAL_LATEST = "evals/reports/eval-latest.json"

app = FastAPI(
    title="OpsPilot 观测面板",
    version=__version__,
    description="工具调用型巡检/诊断 Agent 的本地观测服务（轨迹、证据、护栏、预算）。",
)

_registry = build_registry()
_RUN_LOCK = threading.Lock()  # 见模块 docstring 第 4 条


# --------------------------------------------------------------------- 小工具
def _trace_dir() -> str:
    return get_settings().TRACE_DIR


def _tail_line(path: str) -> dict[str, Any]:
    """只读文件最后一行（summary），不把整条轨迹读进内存。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 8192))
            chunk = fh.read()
        for line in reversed(chunk.splitlines()):
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("{"):
                rec = json.loads(text)
                if rec.get("kind") == "summary":
                    return rec
    except Exception:
        return {}
    return {}


def _head_line(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("{"):
                    return json.loads(line)
    except Exception:
        return {}
    return {}


def _split_records(path: str) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    header: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    events: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            kind = rec.get("kind")
            if kind == "header":
                header = rec
            elif kind == "event":
                events.append(rec)
            elif kind == "summary":
                summary = rec
    return header, events, summary


def _tool_view(decl: Any) -> dict[str, Any]:
    return {
        "name": decl.name,
        "description": decl.description,
        "params": decl.params,                 # 参数名 → 类型名
        "required": decl.required,
        "enum_params": decl.enum_params,
        "evidence_kind": decl.evidence_kind,
        "side_effect": decl.side_effect,
    }


# --------------------------------------------------------------------- 页面
@app.get("/", include_in_schema=False)
def index() -> Response:
    page = WEB_DIR / "index.html"
    if not page.exists():
        raise HTTPException(status_code=500, detail=f"缺少前端文件：{page}")
    return FileResponse(str(page), media_type="text/html; charset=utf-8")


@app.get("/web/{asset:path}", include_in_schema=False)
def static_asset(asset: str) -> Response:
    """web/ 下的附属静态资源。严格限制在 web/ 目录内，防止路径穿越。"""
    target = (WEB_DIR / asset).resolve()
    try:
        target.relative_to(WEB_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400, detail="非法资源路径") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="资源不存在")
    return FileResponse(str(target))


# --------------------------------------------------------------------- 运行
class RunRequest(BaseModel):
    goal: str = Field(..., description="自然语言目标")
    approve: list[str] = Field(default_factory=list, description="已授权的写工具名")
    plan_mode: str | None = Field(default=None, pattern="^(rule|auto|llm)$")


@app.post("/api/run")
def api_run(req: RunRequest) -> JSONResponse:
    """跑一次 Agent，返回完整 TaskResult JSON（含轨迹事件、证据、预算、阻塞项）。"""
    goal = (req.goal or "").strip()
    if not goal:
        raise HTTPException(status_code=400, detail="goal 不能为空")

    known_writes = set(_registry.write_tools())
    # 只接受真实存在的写工具；未知名字直接丢弃并回执，避免"授权清单"里混进脏东西。
    approve = [t for t in req.approve if t in known_writes]
    ignored = [t for t in req.approve if t not in known_writes]

    agent = OpsPilotAgent(_registry, trace_dir=_trace_dir(), record_trace=True)
    if not _RUN_LOCK.acquire(timeout=120):
        raise HTTPException(status_code=503, detail="已有任务在执行，请稍后重试")
    try:
        # approve 原样透传：空列表 = 不授权任何写操作，护栏会把写步骤挂起而不是放行。
        result = agent.run(goal, approve=approve, plan_mode=req.plan_mode)
    finally:
        _RUN_LOCK.release()

    payload = result.to_dict()
    # Evidence 的 positive 是 @property，asdict() 带不出来 → 在服务端补算，供前端过滤。
    payload["evidence"] = [
        {**e, "positive": ev.positive}
        for e, ev in zip(payload["evidence"], result.evidence, strict=True)
    ]
    payload["budget_snapshot"] = result.budget.snapshot()
    payload["approval"] = {"granted": approve, "ignored": ignored}
    return JSONResponse(json.loads(json.dumps(payload, ensure_ascii=False, default=str)))


# --------------------------------------------------------------------- 元信息
@app.get("/api/tools")
def api_tools() -> dict[str, Any]:
    """工具清单：读写分级 + 参数类型 + 必需参数 + 取值域。"""
    decls = [_tool_view(d) for d in _registry.decls()]
    return {
        "total": len(decls),
        "read_count": len(_registry.read_tools()),
        "write_count": len(_registry.write_tools()),
        "read": [d for d in decls if not d["side_effect"]],
        "write": [d for d in decls if d["side_effect"]],
        "manifest_for_llm": len(_registry.prompt_manifest()),
    }


@app.get("/api/health")
def api_health() -> dict[str, Any]:
    s = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "tools": len(_registry.names()),
        # 只看"有没有配 Key"，绝不回显 Key 内容本身。
        "llm_configured": bool(s.LLM_API_KEY),
        "plan_mode": s.PLAN_MODE,
        "write_approval_required": s.WRITE_TOOLS_NEED_APPROVAL,
        "data_dir": s.DATA_DIR,
        "trace_dir": s.TRACE_DIR,
    }


@app.get("/api/data")
def api_data() -> dict[str, Any]:
    """当前演示数据集规模 + 主机清单。"""
    try:
        hosts_raw = data_store.hosts().get("hosts", {})
    except FileNotFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    listing = [
        {
            "id": hid,
            "role": info.get("role", ""),
            "env": info.get("env", ""),
            "region": info.get("region", ""),
            "owner": info.get("owner", ""),
            "services": info.get("services", []),
            "upstream": info.get("upstream", []),
            "downstream": info.get("downstream", []),
        }
        for hid, info in ((h, hosts_raw.get(h) or {}) for h in data_store.host_ids())
    ]
    return {"describe": data_store.describe(), "hosts": listing, "host_ids": data_store.host_ids()}


# --------------------------------------------------------------------- 轨迹
@app.get("/api/traces")
def api_traces() -> dict[str, Any]:
    """最近 20 条轨迹：只读 header 行与 summary 行，不整文件载入。"""
    d = _trace_dir()
    if not os.path.isdir(d):
        return {"trace_dir": d, "traces": [], "total": 0}
    files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jsonl")]
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    out = []
    for p in files[:20]:
        head, summ = _head_line(p), _tail_line(p)
        task_id = head.get("task_id") or Path(p).stem
        out.append({
            "task_id": task_id,
            "mtime": os.path.getmtime(p),
            "size": os.path.getsize(p),
            "goal": head.get("goal", ""),
            "status": summ.get("status", "unknown"),
            "mode": summ.get("mode", ""),
            "budget": summ.get("budget", {}),
            "steps": (summ.get("counters") or {}).get("steps", 0),
            "evidence": (summ.get("counters") or {}).get("evidence", 0),
            "citations": (summ.get("counters") or {}).get("citations", 0),
            "dropped": (summ.get("counters") or {}).get("dropped", 0),
        })
    return {"trace_dir": d, "total": len(files), "traces": out}


@app.get("/api/trace/{task_id:path}")
def api_trace(task_id: str) -> dict[str, Any]:
    # 校验放在任何 IO 之前：只接受 task_<10 hex>，含 / 或 .. 的输入一律 400。
    if not TASK_ID_RE.match(task_id):
        raise HTTPException(status_code=400, detail="非法 task_id：必须匹配 ^task_[0-9a-f]{10}$")
    path = os.path.join(_trace_dir(), f"{task_id}.jsonl")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"轨迹不存在：{task_id}")
    try:
        trace = read_trace(path)
        trace["seq_check"] = "ok"
        return trace
    except ValueError as exc:
        # 已知缺陷（app/agent/trace.py）：header 行不写 seq，事件从 1 开始，
        # 而 read_trace 断言事件应从 2 开始 → 任何轨迹都会判"seq 不连续"。
        # 面板不属于轨迹层，这里降级为"照样渲染、如实标注"，不掩盖问题。
        header, events, summary = _split_records(path)
        return {
            "header": header, "events": events, "summary": summary,
            "seq_check": "degraded", "seq_warning": str(exc),
        }
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"读取失败：{type(exc).__name__}: {exc}") from exc


# --------------------------------------------------------------------- 评测
@app.get("/api/evals")
def api_evals() -> dict[str, Any]:
    """评测概览。文件由评测侧产出，缺失时降级为提示而不是报错。"""
    path = Path(EVAL_LATEST)
    if not path.is_file():
        return {"available": False, "note": "尚未运行评测：python -m app.cli eval"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"available": False, "note": f"评测报告无法解析：{type(exc).__name__}: {exc}"}
    return {"available": True, "path": str(path), "report": data}


if __name__ == "__main__":  # pragma: no cover — 便于 `python -m app.server` 直接起
    import uvicorn

    uvicorn.run(app, host=os.environ.get("OPSPILOT_HOST", "127.0.0.1"),
                port=int(os.environ.get("OPSPILOT_PORT", "18090")))
