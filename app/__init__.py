"""OpsPilot — 巡检与故障诊断智能体（工具调用 Agent 运行时）。

一个不依赖 API Key 也能完整运行的 Agent 运行时：
- planner:     规则 + LLM 双引擎，把自然语言目标拆成带依赖的工具调用计划
- executor:    拓扑序执行、预算与超时真熔断、失败传播、只读步骤重试
- reflection:  跑完判断"证据够不够"，在预算内补只读步骤（重规划）
- guardrails:  输入拦截 / 写操作授权 / 参数消毒 / 输出脱敏 / 连续失败熔断
- evidence:    观察进账本，结论必须引用证据 id
- synthesizer: 规则或 LLM 撰写结论，再过引用闸门（幻觉可计量）
- trace:       opspilot.trace.v1 JSONL，可回放、可审计、可评测

领域是运维巡检与故障诊断；运行时本身与领域无关，换一套工具即可复用。
"""

__version__ = "0.3.0"
