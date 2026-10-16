"""数据装载：巡检数据的唯一入口。

数据是**确定性合成的演示数据**（scripts/seed_demo.py，固定随机种子），
不是任何真实生产环境的数据。工具层只读这里的文件，永不写（写操作走 write.py 的台账）。
缓存按 (路径, mtime) 取，seed 重新生成后不需要重启进程。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

from .config import get_settings

SEED_HINT = "演示数据缺失，请先运行：python scripts/seed_demo.py"


def _ops_path(*parts: str) -> str:
    return os.path.join(get_settings().DATA_DIR, "ops", *parts)


def _load_json(name: str) -> Any:
    path = _ops_path(name)
    if not os.path.exists(path):
        raise FileNotFoundError(SEED_HINT)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_jsonl(name: str) -> list[dict[str, Any]]:
    path = _ops_path(name)
    if not os.path.exists(path):
        raise FileNotFoundError(SEED_HINT)
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


@lru_cache(maxsize=1)
def _noop(_: str) -> None:
    """占位：保留 lru_cache 用法示例，实际缓存策略见 _get（按 mtime 取 key）。"""
    return None


def _get(name: str, loader) -> Any:
    path = _ops_path(name)
    mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    cache_key = f"{name}:{mtime}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    data = loader(name)
    if len(_CACHE) > 48:
        _CACHE.clear()
    _CACHE[cache_key] = data
    return data


_CACHE: dict[str, Any] = {}


def invalidate_cache() -> None:
    _CACHE.clear()


def hosts() -> dict[str, Any]:
    return _get("hosts.json", _load_json)


def host_ids() -> list[str]:
    return sorted(hosts().get("hosts", {}))


def is_known_host(host: str) -> bool:
    return host in host_ids()


def metrics() -> list[dict[str, Any]]:
    return _get("metrics.jsonl", _load_jsonl)


def logs() -> list[dict[str, Any]]:
    return _get("logs.jsonl", _load_jsonl)


def services() -> list[dict[str, Any]]:
    return _get("services.json", _load_json)


def configs() -> dict[str, Any]:
    return _get("configs.json", _load_json)


def incidents() -> list[dict[str, Any]]:
    return _get("incidents_history.jsonl", _load_jsonl)


def runbook_dir() -> str:
    d = _ops_path("runbooks")
    if not os.path.isdir(d):
        raise FileNotFoundError(SEED_HINT)
    return d


def runbooks() -> list[dict[str, Any]]:
    """读 runbooks/*.md，解析 YAML-ish frontmatter（只支持 key: value，够用且零依赖）。"""
    out: list[dict[str, Any]] = []
    d = runbook_dir()
    cache_key = f"runbooks:{len(os.listdir(d))}"
    if cache_key in _CACHE:
        return _CACHE[cache_key]
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(d, fn), encoding="utf-8") as fh:
            text = fh.read()
        meta: dict[str, Any] = {"file": fn}
        body = text
        if text.startswith("---"):
            _, fm, body = text.split("---", 2)
            for line in fm.strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
        meta["body"] = body.strip()
        out.append(meta)
    _CACHE[cache_key] = out
    return out


def describe() -> dict[str, Any]:
    """给 CLI/面板用：当前数据集规模。"""
    try:
        return {
            "hosts": len(host_ids()),
            "metric_points": len(metrics()),
            "log_lines": len(logs()),
            "services": len(services()),
            "runbooks": len(runbooks()),
            "historical_incidents": len(incidents()),
        }
    except FileNotFoundError as exc:
        return {"error": str(exc)}
