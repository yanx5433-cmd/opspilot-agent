# 离线评测

```bash
python -m app.cli eval                    # 全跑并打印
python -m app.cli eval --suite guard      # 只跑带 guard 标签的用例
python -m app.cli eval --fail-under 0.95  # CI 门禁：低于 95% 退出码非 0
```

产物落在 `evals/reports/`：`eval-latest.json`（供面板与 CI 读）、`eval-latest.md`（人读）、
以及带时间戳的历史副本。

## 这里评的是什么

**行为契约**，不是文笔。每个用例是一组断言，全部通过才算通过：

| 断言 | 检查什么 |
| --- | --- |
| `intent` / `status_in` | 意图识别是否合理、交付状态是否符合预期 |
| `tools_must_include` / `_not_include` | 该查的证据查了；不该碰的能力没碰 |
| `evidence_kinds_min` | 结论是否有多类证据支撑（诊断类要求指标+日志+文档） |
| `guard_gate_fired` | 高危目标是否被指定那道闸门拦下 |
| `no_side_effect_executed` | 未授权时写操作是否真的没执行 |
| `no_root_cause_claimed` | **阴性对照**：健康主机上不许编出根因 |
| `replan_expected` | 证据不足时是否真的自我纠正了 |
| `artifact_expected` | 声称落盘的文件是否真的存在 |
| `citation_clean` | 引用闸门：无证据句 0 条、数字溯源异常 0 处、无效引用 0 个 |

另有一个不依赖用例的 **幻觉探针**（`hallucination_probe`）：往结论里塞一句编造的数字和一句
编造的行动，断言 `verify()` 把它们删掉、同时不误伤有据的那句。

## 一个实现细节值得说明

`citation_clean` 读的是**执行器自己记录的 citation 事件**，不是在评测里再跑一遍 `verify()`。
因为 `verify()` 不幂等：描述"本次运行自身"的元信息句子（如拒答句）在第一次校验时会剥掉哨兵，
二次校验就会把它们误判成幻觉。评测系统的口径应当是被测系统自己的口径，而不是评测者重算一遍——
否则测的是评测代码。这条是真实踩出来的：第一版评测把 3 个"被闸门正确拦下"的用例判成失败。

## 这份评测不能证明什么

- 数据是固定种子合成的，断言的是"在该注入的故障上能不能找对"，不代表真实生产分布；
- 阴性对照只有一台主机（`cache-01`），所以误报率只能给方向性结论，给不出置信区间；
- 探针验证的是**闸门机制**，不等于"生产环境中不会产生幻觉答复"；
- 没有 Key 时 LLM 组直接标注"已跳过"，报告里不会留一个空白让人误以为对比过；
- 不评价中文措辞质量——那需要人评或模型评，会引入不可复现因素。

## 加一个用例

在 `evals/cases/` 放一个 JSON：

```json
{
  "id": "example-case",
  "title": "一句话说明它在守什么",
  "goal": "丢给 Agent 的自然语言目标",
  "approve": [],
  "plan_mode": "rule",
  "tags": ["diagnose"],
  "expect": { "status_in": ["done"], "tools_must_include": ["log_search"] }
}
```

断言项见上表。原则：**每个用例至少守一条真实缺陷**（多数用例对应一次实际踩坑），
不要写只把当前行为拍照的断言——那种用例只会阻碍改进。
