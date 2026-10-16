"""集中配置：预算、护栏、LLM 接入。全部可用 OPSPILOT_ 前缀环境变量覆盖。

设计原则：默认值必须让系统"零依赖可跑"——不配任何 API Key 时，
规则引擎接管全部决策，功能完整度不降级（只是答案的文风变朴素）。
"""
from __future__ import annotations

import os


def _env(name: str, default: str) -> str:
    return os.environ.get(f"OPSPILOT_{name}", default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_flag(name: str, default: str = "1") -> bool:
    return _env(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    """运行时设置（进程内单例：get_settings()）。"""

    # --- LLM：可选增强。未配置 api_key 时 planner/answer 自动降级到规则引擎 ---
    LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "")
    LLM_BASE_URL: str = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    LLM_MODEL: str = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    LLM_TIMEOUT_SEC: float = _env_float("LLM_TIMEOUT_SEC", 15.0)
    LLM_MAX_RETRY: int = _env_int("LLM_MAX_RETRY", 2)
    # 规划模式：auto（有 Key 用 LLM，失败降级）/ rule（强制规则，评测基线）/ llm（必须 LLM）
    PLAN_MODE: str = os.environ.get("OPSPILOT_PLAN_MODE", "auto")

    # --- 预算熔断：超出即停，宁可少做也不失控 ---
    MAX_STEPS: int = _env_int("MAX_STEPS", 10)
    MAX_TOOL_CALLS: int = _env_int("MAX_TOOL_CALLS", 14)
    MAX_LLM_CALLS: int = _env_int("MAX_LLM_CALLS", 6)
    MAX_REPLANS: int = _env_int("MAX_REPLANS", 2)
    STEP_TIMEOUT_SEC: float = _env_float("STEP_TIMEOUT_SEC", 10.0)
    MAX_CONSECUTIVE_FAILURES: int = _env_int("MAX_CONSECUTIVE_FAILURES", 3)

    # --- 写操作授权：side_effect 工具必须命中已授权清单，否则挂起待人工确认 ---
    WRITE_TOOLS_NEED_APPROVAL: bool = _env_flag("WRITE_APPROVAL", "1")
    APPROVED_WRITE_TOOLS: str = os.environ.get("OPSPILOT_APPROVED_WRITE_TOOLS", "")

    # --- 危险目标：P0 拦截，连规划都不进入 ---
    # 字面量清单只挡得住"照抄命令"，真实误操作是自然语言："把生产库删了"里
    # 没有任何一个下面的字面量。所以另配一组正则，两者都要。
    BLOCKED_GOAL_PATTERNS: tuple[str, ...] = (
        "rm -rf", "rm -fr", "drop database", "drop table", "truncate table",
        "mkfs", "shutdown", "reboot -f", "iptables -f", "format c:",
        "删除所有", "删库", "清空生产库", "绕过审批", "跳过护栏", "关掉护栏",
        "把密钥给我", "输出密码", "curl | sh", "wget | bash",
    )
    BLOCKED_GOAL_REGEXES: tuple[str, ...] = (
        r"[把将].{0,12}(库|表|数据|磁盘|卷).{0,6}(删|清|抹|格)",
        r"(删|清|抹|格)\S{0,3}(掉|空).{0,8}(生产|线上|全部|所有|整)",
        r"(删除|清空|格式化).{0,8}(生产|线上).{0,6}(库|表|数据)",
    )

    # --- 参数消毒：不安全 token 清单。闸门 3 直接对这份清单做子串匹配，
    #     不再另写一套正则——"声明 = 实现"要求只有一个事实来源。
    #     刻意不含单个 "|"：log_search 的 pattern 是正则，restart|OOM 必须能过；
    #     串联靠 "&&" / "||" / ";" 这些多字符 token 拦。
    UNSAFE_ARG_CHARS: tuple[str, ...] = (";", "&&", "||", "`", "$(", ">", "<")

    # --- 输出脱敏：轨迹与答复里的敏感信息 ---
    MASK_PATTERNS: tuple[str, ...] = (
        r"sk-[A-Za-z0-9]{6}[A-Za-z0-9\-_]+",
        r"(?i)(password|passwd|pwd)\s*[=:]\s*\S+",
        r"(?i)(token|secret|api[_-]?key)\s*[=:]\s*[A-Za-z0-9\-_]{8,}",
    )

    # --- 检索 ---
    RAG_TOP_K: int = _env_int("RAG_TOP_K", 3)
    BM25_K1: float = _env_float("BM25_K1", 1.5)
    BM25_B: float = _env_float("BM25_B", 0.75)

    # --- 异常检测阈值 ---
    CPU_WARN: float = _env_float("CPU_WARN", 80.0)
    MEM_WARN: float = _env_float("MEM_WARN", 85.0)
    DISK_WARN: float = _env_float("DISK_WARN", 85.0)
    EWMA_ALPHA: float = _env_float("EWMA_ALPHA", 0.3)
    EWMA_SIGMA: float = _env_float("EWMA_SIGMA", 3.0)

    # --- 磁盘写满预测：最少需要多少个观测点才敢外推 ---
    FORECAST_MIN_POINTS: int = _env_int("FORECAST_MIN_POINTS", 6)
    FORECAST_HORIZON_DAYS: int = _env_int("FORECAST_HORIZON_DAYS", 30)

    # --- 路径 ---
    DATA_DIR: str = os.environ.get("OPSPILOT_DATA_DIR", "data")
    TRACE_DIR: str = os.environ.get("OPSPILOT_TRACE_DIR", "run/traces")
    ARTIFACT_DIR: str = os.environ.get("OPSPILOT_ARTIFACT_DIR", "run/artifacts")
    EVAL_REPORT_DIR: str = os.environ.get("OPSPILOT_EVAL_REPORT_DIR", "evals/reports")

    # --- 证据约束：答复里允许出现的最大"无证据句"比例，超出即判定不可信 ---
    MAX_UNEVIDENCED_RATIO: float = _env_float("MAX_UNEVIDENCED_RATIO", 0.2)


_settings = Settings()


def get_settings() -> Settings:
    return _settings
