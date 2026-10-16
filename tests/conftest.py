"""pytest 全局夹具：离线、确定性、零副作用。

三条硬约束（整个套件的可信度都建立在这上面）：
1. **不联网**：强制 LLM_API_KEY 为空 → planner/answer 一律走规则引擎，
   任何一次运行都不产生 HTTP 请求，CI 在无外网的容器里也能跑出同样的数；
2. **不污染仓库**：轨迹与交付物写到每个测试自己的 tmp_path，
   只有 data/ops（只读输入）是共享的；
3. **数据确定性**：data/ops 缺失时调用 scripts/seed_demo.py 的写盘函数重建，
   种子固定（SEED=20261017），所以任何断言里的数字都是可复现的。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import data_store
from app.config import get_settings
from app.tools import (
    build_registry,
    kb,
)

DATA_OPS = ROOT / "data" / "ops"


def _seed_if_missing() -> None:
    """data/ops 不存在（或残缺）时重新生成演示数据集。"""
    required = ["hosts.json", "metrics.jsonl", "logs.jsonl", "services.json", "configs.json",
                "incidents_history.jsonl", "runbooks"]
    if all((DATA_OPS / name).exists() for name in required):
        return
    from scripts.seed_demo import write_all

    write_all(str(DATA_OPS))


@pytest.fixture(scope="session", autouse=True)
def seeded_dataset() -> dict[str, int]:
    """整个会话只准备一次数据；测试全程只读它。"""
    _seed_if_missing()
    data_store.invalidate_cache()
    kb.reset_index()
    desc = data_store.describe()
    assert "error" not in desc, f"演示数据不可用：{desc}"
    return desc


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch, tmp_path: Path) -> None:
    """每个测试都强制：无 Key、规则引擎、零写授权、产物落到自己的 tmp。

    这里把所有会被测试改动的设置项**全部**过一遍 monkeypatch，
    包括测试里可能直接赋值改掉的 LLM_* —— monkeypatch 在 teardown 时
    会把类属性上的原值还原，从而杜绝"上一个测试改了全局单例"的交叉污染。
    """
    s = get_settings()
    for name, value in {
        "LLM_API_KEY": "",
        "LLM_BASE_URL": "https://llm.invalid/v1",
        "LLM_MODEL": "unit-test-model",
        "LLM_TIMEOUT_SEC": 5.0,
        "LLM_MAX_RETRY": 0,
        "PLAN_MODE": "rule",
        "APPROVED_WRITE_TOOLS": "",
        "WRITE_TOOLS_NEED_APPROVAL": True,
        "MAX_REPLANS": 2,
        "MAX_TOOL_CALLS": 14,
        "MAX_STEPS": 10,
        "MAX_LLM_CALLS": 6,
        "STEP_TIMEOUT_SEC": 10.0,
        "MAX_CONSECUTIVE_FAILURES": 3,
        "FORECAST_MIN_POINTS": 6,
        "TRACE_DIR": str(tmp_path / "traces"),
        "ARTIFACT_DIR": str(tmp_path / "artifacts"),
    }.items():
        monkeypatch.setattr(s, name, value, raising=False)


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def registry():
    """每次给一个全新的注册表，避免测试之间互相影响工具状态。"""
    return build_registry()


@pytest.fixture
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def no_network(monkeypatch):
    """把出站 HTTP 全部钉死：谁调谁报错。用于证明"拒绝路径不会真的发包"。"""
    import urllib.request

    def _blocked(*_a, **_kw):
        raise AssertionError("测试期间不允许发生任何真实网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    return _blocked
