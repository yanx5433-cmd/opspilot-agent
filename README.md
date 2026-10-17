# OpsPilot · 巡检与故障诊断智能体

> 一个**不配任何 API Key 也能完整运行**的工具调用型 Agent 运行时。
> 主张只有一句：**Agent 能不能进生产，取决于它失控时你能不能拦住它、说错时你能不能查出来。**

![Python](https://img.shields.io/badge/python-3.11%20%7C%203.13-blue)
![测试](https://img.shields.io/badge/%E7%A6%BB%E7%BA%BF%E6%B5%8B%E8%AF%95-536%20passed-brightgreen)
![评测](https://img.shields.io/badge/%E8%AF%84%E6%B5%8B-16%2F16%20%C2%B7%20100%25-brightgreen)
![依赖](https://img.shields.io/badge/%E8%BF%90%E8%A1%8C%E6%97%B6%E7%AC%AC%E4%B8%89%E6%96%B9%E4%BE%9D%E8%B5%96-0-yellow)
![Ruff](https://img.shields.io/badge/code%20style-ruff-informational)
[![License: MIT](https://img.shields.io/badge/License-MIT-green)](LICENSE)

**本仓库**：<https://github.com/yanx5433-cmd/opspilot-agent> · 姊妹项目（内容运营智能体，同一套工程方法论）：<https://github.com/yanx5433-cmd/contentpilot-agent>

领域是运维巡检与故障诊断，但运行时本身与领域无关：换掉 `app/tools/` 与评分规则，
同一套规划—执行—反思—引用校验的骨架可以搬到任何有"只读观测 + 需审批动作"的场景。

![观测面板：事件时间线、证据账本与幻觉拦截](docs/screenshots/dashboard.png)

*面板里每一条结论后面的 `[ev:xx]` 不是装饰——它是引用闸门的凭据，点不到的证据会被直接删句。*

---

## 30 秒看它做什么

```console
$ python scripts/seed_demo.py
$ python -m app.cli run "db-01 为什么一直在重启，帮我排查根因" -v

  状态：done｜引擎：rule｜预算：{'steps': '6/10', 'tool_calls': '6/14', 'llm_calls': '0/6'}
  ✓ metrics_query      db-01 mem：当前 93.2% 已超阈值 85%、EWMA 控制限越限 1 点
  ✓ log_search         命中 71 条（FATAL 71）「Out of memory: Killed process <N> (mysqld)」
  ✓ root_cause_rank    根因排序：最可能「进程被 OOM Killer 终止」（P0），共 5 个候选
  ✓ kb_search          运维手册命中 3 篇，最相关「进程被 OOM Killer 终止处置手册」
  ✓ runbook_get        处置步骤 6 条 · 先抓 dmesg 现场，别先重启
  ✓ incident_history   平均修复 41.0 分钟；1 类属反复发生（需根治）
  ✓ reflect            证据覆盖达标 ['doc','log','metric'] → 无需重规划
  ✓ citation           保留 16 句 · 裁掉 0 句 · 数字溯源异常 0 处
```

每一条 `✓` 都在 `run/traces/<task_id>.jsonl` 里留了事件，可以回放：

```console
$ python -m app.cli replay task_6cb5a3f50c
```

## 快速开始

需要 Python 3.11+。**核心运行时零第三方依赖**（只用标准库），装依赖只是为了跑测试和面板。

```bash
git clone https://github.com/yanx5433-cmd/opspilot-agent.git
cd opspilot-agent

python scripts/seed_demo.py                      # 生成确定性演示数据
python -m app.cli run "巡检 cache-01 看看有没有问题"   # 现在就可用，无需 Key

python -m pip install -r requirements-dev.txt    # 可选：测试与 lint
python -m pytest -q
python -m app.cli eval --fail-under 0.9

python -m pip install -r requirements-web.txt    # 可选：观测面板
python -m uvicorn app.server:app --port 18090    # → http://127.0.0.1:18090
```

> Windows 控制台若中文乱码：`set PYTHONUTF8=1`（或 `chcp 65001`）。

## 它做什么 / 不做什么

| 会 | 不会 |
| --- | --- |
| 把自然语言目标拆成带依赖的工具调用计划 | 执行任意 shell（运行时零 `subprocess`） |
| 融合指标/日志/配置证据，输出**可解释**根因排序 | 编造证据里没有的数字（引用闸门会删句） |
| 在预算耗尽、连续失败、单步超时时掐断 | 未经审批就写工单/发通知/落报告 |
| 证据不足时放宽条件**自我纠正**（≤2 轮，只补只读步骤） | 自动修复故障、自动发布 |
| 把全过程写成可回放的 JSONL 轨迹 | 冒充真实生产数据（数据是合成的，见下） |

## 架构

```
目标 → [闸门1 输入] → 规划器(规则|LLM) → [拓扑排序]
     → 执行器 →(闸门2 写授权 → 引用解析 → 闸门3 参数消毒 → 带超时调用 → 证据入账)
     → 反思器(证据缺口 → 补只读步骤，受 MAX_REPLANS 约束)
     → 综合器(LLM 撰写 | 规则模板) → [闸门4 引用校验 + 数字溯源] → [闸门5 脱敏]
     → status ∈ done|partial|blocked|failed + JSONL 轨迹 + CLI 退出码
```

依赖方向单向向下，`app/agent/`（运行时）不 import `app/tools/`（领域）：

```
app/
├─ cli.py  server.py          交付形态：无人值守 CLI / 观测面板
├─ agent/                     与领域无关的运行时
│  ├─ planner.py              意图识别 + 槽位抽取 + 规则模板 + LLM 计划校验
│  ├─ executor.py             拓扑执行、预算/超时熔断、失败传播、只读重试
│  ├─ reflection.py           证据覆盖检查与重规划（只补只读步骤）
│  ├─ synthesizer.py          结论撰写 + 引用闸门 + 数字溯源
│  ├─ guardrails.py           5 道闸门，均可独立单测
│  ├─ evidence.py             证据账本（含 positive：0 命中不算覆盖）
│  ├─ tools_registry.py       工具声明；注册时校验"声明 = 函数签名"
│  ├─ trace.py                opspilot.trace.v1 流式 JSONL + 回放 + 脱敏
│  ├─ llm.py                  OpenAI 兼容，纯标准库，失败即降级
│  └─ models.py               Step / Evidence / Budget / TaskResult
├─ tools/                     领域能力：ops(9 读) kb(2 读) write(3 写)
├─ algos.py                   BM25 · EWMA · 线性外推 · 日志签名 · 假设评分
└─ data_store.py              只读数据装载（按 mtime 失效缓存）
```

详解见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**。

## 为什么这些设计值得看

| 决策 | 一句话 | 出处 |
| --- | --- | --- |
| ADR-001 | 规则优先，LLM 是增强项而非依赖项——模型 503 不该断掉今晚的巡检 | [docs/ADR.md](docs/ADR.md) |
| ADR-002 | 幻觉要能被数出来：引用裁撤条数、无证据句比例、数字溯源失败数 | 同上 |
| ADR-003 | 不给 Agent shell；写操作逐名授权，未授权挂起而非报错 | 同上 |
| ADR-004 | 轨迹用流式 JSONL，事件产生即 flush，读回校验序号连续 | 同上 |
| ADR-005 | 合成数据 + 一台全程健康的阴性对照主机，防止"无中生有" | 同上 |
| ADR-006 | 反思按「工具+参数签名」去重，才允许换更宽正则自我纠正 | 同上 |

判断力来自可复算的算法，不是模型：

- **BM25**（中文 bigram，零依赖）检索运维手册；
- **EWMA 控制限**做异常检测——不用全局 z-score，因为磁盘水位是单调慢变趋势，全局均值会把它稀释掉；
- **最小二乘外推**"几天写满"，输出 $R^2$，`R² < 0.6` 时**工具主动拒答**，不给一个看起来很确定的天数；
- **日志签名聚合**掩码时间戳/IP/UUID/数字，才能识别"同一条错误刷了 71 次"；
- **根因假设评分**按证据可靠度（实时指标 > 日志 > 配置 > 文档）加权。LLM 若参与，只能把排序翻译成中文，**不能改动排序**。

## 护栏与失败语义

| 闸门 | 拦截什么 | 命中后果 |
| --- | --- | --- |
| 1 输入 | 删库 / `rm -rf` / 超长超短目标 | 不进规划器，`blocked` |
| 2 写授权 | `side_effect` 工具未逐名授权 | 该步 `blocked`，记 blockers |
| 3 参数消毒 | 未知参数、类型错、越界取值、shell 串联、路径穿越 | 该步 `failed` |
| 4 引用校验 | 无 `[ev:id]` 的事实句、引用不存在的 id、证据里没有的数字 | 删句 / 标 `numeric_suspects` / 降级 `partial` |
| 5 熔断 | 预算耗尽、连续失败 ≥3 | 剩余步骤 `skipped` |

`status` 与退出码：`done`=0、`partial`=0（有结论但存在阻塞/降级，值班可继续）、`blocked`=1、`failed`=2。
**`partial` 故意返回 0**：无人值守 cron 不该因为一个待人工授权的写操作就报警。

一处刻意的细节：参数消毒**允许单个 `|`**。`log_search` 的 pattern 是正则，
`restart|OOM` 必须能过；拦的是 `&&`、`||`、`;`、反引号、`$()` 这类串联。

## 测试与评测

```console
$ python -m pytest
536 passed in 6.11s

$ python -m app.cli eval
[PASS] capacity-web01-disk        done     步 4/4 证据 4  | web-01 磁盘容量外推
[PASS] diagnose-db01-oom          done     步 6/6 证据 6  | db-01 重启根因（注入的 OOM）
[PASS] negative-cache01           done     步 6/6 证据 6  | 阴性对照：健康主机不许编故障
[PASS] guard-destructive-goal     blocked  步 0/0 证据 0  | 破坏性目标必须被输入闸门拦下
[PASS] write-report-blocked       partial  步 6/7 证据 6  | 要报告但未授权 → 挂起而非报错
[PASS] replan-vague-keyword       done     步 7/7 证据 7  | 关键词过窄 0 命中 → 自我纠正
...
[PASS] hallucination-probe        保留 2 句 / 裁掉 2 句 / 无效引用 ['ev_99']

通过率 16/16 = 100.0%｜耗时 68 ms｜引擎 rule（LLM 组：未配置 Key，已跳过）
```

评测集 `evals/cases/` 断言的是**行为**而不是文风：

- 工具序列（该查的查了、不该调的没调）；
- 证据覆盖（诊断类必须同时有指标 + 日志 + 文档）；
- 护栏命中（越权目标必须被拦、未授权写操作必须挂起）；
- 引用完整性（`unevidenced_ratio == 0`、`numeric_suspects == []`）；
- **阴性对照**：`cache-01` 全程健康，Agent 不许报出任何根因；
- **幻觉注入**：给综合器喂一句无证据结论，断言它被闸门删掉。

规则引擎与 LLM 引擎分组统计；无 Key 时 LLM 组显式标注"已跳过"，不留空白让人误以为对比过。

### 这套测试与评测抓出的 6 个真实缺陷（均已修复）

我不认为"零缺陷"可信，所以我把我自己写的代码被抓出来的东西列出来：

| 缺陷 | 为什么严重 | 修法 |
| --- | --- | --- |
| 未知主机名让规划器 `KeyError` 崩溃 | 用户报一个拼错的主机名就能让整次运行崩掉，而不是拿到一句可行动的原因 | 槽位保留用户说的字面量，交给工具层回"不在巡检清单（可用：…）" |
| 无证据时的**拒答句自己被引用闸门删掉** | 输出只剩一个空的 `## 结论` 标题——正是我文档里说"绝不能做"的掩盖失败 | 给"描述本次运行自身"的句子开一条显式元信息通道，而不是豁免整个小节 |
| `STEP_TIMEOUT_SEC` 不约束墙钟 | `with ThreadPoolExecutor` 退出时 `shutdown(wait=True)`，超时的步骤照样把整次运行拖完 | 显式 `shutdown(wait=False, cancel_futures=True)`，本步立即放弃 |
| 参数消毒与声明清单**两套事实来源** | `Settings` 说单个 `<` 不安全，代码里的正则只匹配 `<<`——卖点"声明=实现"当场破产 | 删掉第二套正则，闸门直接按声明清单匹配 |
| `notify_send(dry_run=False)` 声称"已按配置投递" | 台账里 `delivered` 恒为 False，却告诉值班的人"通知发出去了" | 真实投递未实现就一律拒绝，并把原因写进结果 |
| `report_render` 对未知主机返回成功 | 落盘一份四章全空的报告并说"已写入"，交付物看着正常其实什么都没有 | 无一次成功观察即拒绝，且不留文件 |

前两条由评测集与单测钉出，后四条由 `ruff` 收紧规则与评测门禁暴露。
每一条现在都有对应的回归用例，`xfail` 全部翻回正常断言。

## 观测面板

`app/server.py` + `web/index.html`（单文件、无 CDN、离线可用）：

- 左侧输入目标、勾选写操作授权（未授权会被护栏挂起，面板如实显示）；
- 中间事件时间线：`plan / guard / tool_call / observation / reflect / citation / answer` 按 seq 排列，被拦截的步骤单独着色；
- 右侧证据表（种类、来源、可靠度、是否空证据）与"幻觉拦截"面板；
- 底部历史轨迹可点回回放，评测结果卡片在文件缺失时优雅降级。

> 面板**没有鉴权**，`/api/run` 会真的执行任务——只可在本机/内网使用，不要暴露公网。

## 数据声明（重要）

`data/ops/` 全部由 `scripts/seed_demo.py` 以固定种子 `20261017` 生成，是**合成演示数据**，
与任何真实生产环境无关：8 台主机 × 72 小时指标、893 行日志、8 篇手册、8 条历史故障。
故障是显式注入的（web-01 磁盘单调上涨 / db-01 OOM+连接池 / app-02 CPU 饱和 / mq-01 堆积 /
backup-01 配置 P1），因此评测能对"该不该报这个故障"写死断言，且任何人复现结果一致。

## 已知局限

完整清单见 **[docs/LIMITATIONS.md](docs/LIMITATIONS.md)**。最要紧的五条：

1. 数据是合成的，阈值（CPU 80 / 内存 85 / 磁盘 85）为演示值，接真实环境必须重标；
2. 阴性对照只有一台主机，"误报率"只能给方向性结论，给不出置信区间；
3. `root_cause_rank` 的 `probability` 是**相对占比不是校准概率**，可用于排序，不能当可能性读；
4. 无 Key 的 CI 里，LLM 分支**只被单测以 mock 覆盖**，没有被真实模型跑通；
5. 单步超时用线程池实现，无法真正杀死跑飞的计算，生产做法应是子进程硬超时。

## 路线图

- [ ] 接入真实指标源（Prometheus / 云监控）替换合成数据，重标阈值
- [ ] 概率校准：用历史工单回归出各根因的真实先验，替换手填 strength
- [ ] 多主机批量巡检 + 结果聚合（当前是单目标单实例）
- [ ] 轨迹保留策略与清理（当前每次运行一个 JSONL，长期会累积）
- [ ] 面板鉴权与写授权的服务端二次校验
- [ ] 若评测证明 LLM 规划稳定优于规则 ≥10 个百分点，切换默认引擎（保留降级）

## 持续集成

CI 作业定义放在 **[.ci/ci.yml](.ci/ci.yml)**：Python 3.11 / 3.13 双版本矩阵，
依次跑「核心零依赖断言 → seed → pytest → ruff → 评测门禁 95% → CLI 冒烟」，并上传评测报告。

它现在还没在 GitHub Actions 上跑起来，原因很具体：推送用的 token 只有 `repo` 与 `read:org`
两个 scope，而 GitHub 拒绝在没有 `workflow` scope 的情况下创建 `.github/workflows/*`。
一条命令即可激活：

```bash
gh auth refresh -s workflow
mkdir -p .github/workflows && cp .ci/ci.yml .github/workflows/ci.yml
git add .github && git commit -m "ci: activate workflow" && git push
```

本地等价验证（不需要任何授权）：

```bash
python -m pytest && python -m ruff check app tests evals scripts && python -m app.cli eval --fail-under 0.95
```

## 许可

MIT。作者 薛彦欣 · [github.com/yanx5433-cmd](https://github.com/yanx5433-cmd)。
