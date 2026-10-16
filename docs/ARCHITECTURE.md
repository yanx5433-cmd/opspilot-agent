# OpsPilot 架构说明

本文讲"为什么这么分层"，不是 API 手册（API 见 `app/` 源码与 `--help`）。

## 一次请求的完整生命周期

```
用户目标（自然语言）
      │
      ▼
┌──────────────┐  闸门1 输入拦截（删库/rm -rf/超长/过短）
│  Guardrails  │────────────────────────────────────────┐
└──────────────┘                                         │命中即 blocked
      │                                                  │并落 trace，不进规划器
      ▼
┌──────────────┐   意图识别 → 槽位抽取 → 计划模板
│   Planner    │   PLAN_MODE=auto 时先问 LLM 要 JSON 计划，
│  rule / llm  │   逐条校验（未知工具丢弃、超预算截断、依赖重映射）；
└──────────────┘   校验失败或无 Key → 回落规则模板
      │ steps[{tool,args,reason,depends_on}]
      ▼
┌──────────────┐   Kahn 拓扑排序（环/悬空依赖 → fail fast）
│  Executor    │   逐步：预算检查 → 依赖失败传播 → 闸门2 写授权
│              │   → 上游引用解析($id.field / $evid:id) → 闸门3 参数消毒
│              │   → 带超时调用 → 只读失败重试1次 → 观察进证据账本
└──────────────┘
      │ observations
      ▼
┌──────────────┐   按意图要求的证据种类做覆盖检查；
│ Reflection   │   有缺口且预算允许 → 补**只读**步骤（最多 MAX_REPLANS 轮）
└──────────────┘
      │
      ▼
┌──────────────┐   LLM 撰写（只喂证据 JSON）→ 失败回落规则模板
│ Synthesizer  │
└──────────────┘
      │ draft
      ▼
┌──────────────┐   闸门4 引用校验：无 [ev:id] 的事实句删除；
│ Citation Gate│   引用不存在的 id → 剔除并计数；
│              │   句中出现证据里不存在的数字 → 标为 numeric_suspects
└──────────────┘
      │
      ▼
  status ∈ done / partial / blocked / failed + JSONL 轨迹 + CLI 退出码
```

## 分层与依赖方向

```
app/cli.py  app/server.py        ← 交付形态（无人值守 / 观测面板）
    └──────────┐
       app/agent/*               ← 运行时：与领域无关
         planner reflection executor synthesizer
         guardrails evidence tools_registry trace llm models
       app/tools/*               ← 领域能力：运维巡检
       app/algos.py              ← 判断力：BM25 / EWMA / 回归 / 签名聚合 / 假设评分
       app/data_store.py         ← 数据装载（只读）
       data/ops/*                ← 确定性演示数据
```

依赖只能向下。`app/agent/` 不 import `app/tools/`，所以把这套运行时搬到别的领域
（内容运营、电商客服、数据库体检）只需要换 `app/tools/` 与 `app/algos.py` 里的评分规则。

## 三处关键契约

**1. 工具返回契约。** 每个工具必须返回

```python
{"ok": bool, "summary": "一句话结论", "evidence_kind": "metric|log|probe|config|doc|ticket|report",
 "source": "可回溯的位置，如 metrics:web-01:disk", **data}
```

`summary` 是能被写进结论的那句话，`source` 让结论可回溯。`ToolRegistry.register()`
会用 `inspect.signature` 比对声明参数与函数签名，不一致直接抛错——**声明漂移是
运行时 bug，不是文档问题**，所以在注册时就拦。

**2. 证据契约。** 观察经 `harvest()` 进 `EvidenceLedger`，拿到 `ev_01` 这样的 id。
`Evidence.positive` 区分"真的查到了"和"查了但没有"：检索 0 命中是**有价值的观察**
（答复里要写"未匹配到日志"），但不算覆盖了该证据种类，否则反思器永远认为证据齐了。

**3. 轨迹契约 `opspilot.trace.v1`。** 每行一个 JSON：`header` → N 个 `event` → `summary`。
事件类型：`guard / plan / llm / tool_call / observation / reflect / citation / answer`。
`read_trace()` 校验 seq 连续性，缺行即报错——审计日志有洞必须当场发现，
而不是等到复盘时才发现少了一段。

## 预算与失败语义

| 维度 | 配置项 | 耗尽后果 |
| --- | --- | --- |
| 步骤数 | `MAX_STEPS=10` | 后续步骤 `skipped` |
| 工具调用 | `MAX_TOOL_CALLS=14` | 同上，且熔断检查优先 |
| LLM 调用 | `MAX_LLM_CALLS=6` | 综合阶段直接走规则模板 |
| 重规划轮数 | `MAX_REPLANS=2` | 记录缺口但不补步骤 |
| 单步耗时 | `STEP_TIMEOUT_SEC=10` | 该步 `failed`（线程池掐断） |
| 连续失败 | `MAX_CONSECUTIVE_FAILURES=3` | 全局熔断，剩余步骤 `skipped` |

| status | 含义 | CLI 退出码 |
| --- | --- | --- |
| `done` | 全部步骤成功且有证据，引用闸门无异常 | 0 |
| `partial` | 有可用结论，但存在被拦截/跳过/失败的步骤，或数字无法溯源 | 0（可继续，但要看 blockers） |
| `blocked` | 护栏拦下了关键步骤，没有可用结论 | 1 |
| `failed` | 计划不合法或全部步骤失败 | 2 |

`partial` 用退出码 0 是刻意的：无人值守 cron 里，"磁盘已告警 + 报告待授权"是有价值的
一晚，不该因为一个待授权写操作就报警给值班的人。真正的失败才回非 0。

## 双引擎与降级路径

```
PLAN_MODE=auto（默认）
  ├─ 无 LLM_API_KEY ────────────────→ 规则模板（engine=rule）
  ├─ 有 Key，模型返回合法 JSON 计划 ─→ 校验后执行（engine=llm）
  └─ 有 Key，但超时/非法 JSON/全是未知工具 → 回落规则（engine=llm→rule，轨迹里记 fallback_reason）
```

答复侧同理：`llm_answer()` 失败即 `rule_answer()`。**任何一次模型故障都不会让今晚的巡检失败**，
这是设计目标而不是容错装饰。规则引擎因此不是"占位实现"，它必须达到可用的交付质量——
评测集默认就跑在规则引擎上，这样基线是可信的。

## 加一个工具要改几处

1. `app/tools/<domain>.py` 写实现，返回上面那个契约；
2. `app/tools/__init__.py` 加一条 `ToolDecl`（参数、必需项、取值域、`side_effect`、`evidence_kind`）；
3. 需要被规划器选到时，在 `app/agent/planner.py` 的意图模板或 `reflection.GAP_TOOLS` 里加一步；
4. `tests/test_tools.py` 补成功路径 + 至少一条错误路径；`evals/cases/` 补一个金标用例。

写操作工具额外要做的事：把风险写进 `description`，让授权清单能按名字精确授予
（`--approve report_render` 就是字符串匹配，粒度是"单个工具"，不是"写操作"这个大类）。
