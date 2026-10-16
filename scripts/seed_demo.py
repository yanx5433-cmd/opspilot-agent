"""生成确定性演示数据集（data/ops/）。

为什么强调确定性：随机种子固定 + 异常显式注入，所以
① 评测集可以对"Agent 该不该报这个故障"写死断言；
② CI 与面试官本地跑出来的结果一致，不存在"我这儿是绿的"；
③ 数据是合成的，不冒充生产数据——README 与报告落款都会写明这一点。

注入的故障（写死，不靠随机）：
- web-01    磁盘单调上涨 62%→93%（可外推，R² 高）+ 日志轮转未开
- db-01     内存高位 + OOM Killer + 连接池耗尽日志
- app-02    CPU 饱和且 EWMA 越限
- mq-01     消息堆积 + 下游超时
- backup-01 备份开关为关 + 证书 18 天后到期（配置类 P1）
- cache-01  全程健康（阴性对照：Agent 不该无中生有）
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
from pathlib import Path

SEED = 20261017
HOURS = 72
BASE_TIME = dt.datetime(2026, 10, 16, 0, 0, 0)

HOSTS = {
    "web-01": {"role": "nginx", "env": "prod", "region": "cn-hangzhou", "owner": "平台组",
               "ip": "10.12.3.11", "services": ["nginx", "app-order"],
               "upstream": [], "downstream": ["app-01", "app-02"]},
    "web-02": {"role": "nginx", "env": "prod", "region": "cn-hangzhou", "owner": "平台组",
               "ip": "10.12.3.12", "services": ["nginx", "app-user"],
               "upstream": [], "downstream": ["app-01"]},
    "app-01": {"role": "app", "env": "prod", "region": "cn-hangzhou", "owner": "交易组",
               "ip": "10.12.3.21", "services": ["app-order", "app-user"],
               "upstream": ["web-01", "web-02"], "downstream": ["db-01", "cache-01"]},
    "app-02": {"role": "app", "env": "prod", "region": "cn-shanghai", "owner": "交易组",
               "ip": "10.12.4.22", "services": ["app-pay"],
               "upstream": ["web-01"], "downstream": ["db-01", "mq-01"]},
    "db-01": {"role": "mysql", "env": "prod", "region": "cn-hangzhou", "owner": "DBA",
              "ip": "10.12.3.31", "services": ["mysqld", "mysqld-exporter"],
              "upstream": ["app-01", "app-02"], "downstream": ["backup-01"]},
    "cache-01": {"role": "redis", "env": "prod", "region": "cn-hangzhou", "owner": "平台组",
                 "ip": "10.12.3.41", "services": ["redis-server"],
                 "upstream": ["app-01"], "downstream": []},
    "mq-01": {"role": "rabbitmq", "env": "prod", "region": "cn-shanghai", "owner": "平台组",
              "ip": "10.12.4.51", "services": ["beam-sasl", "mq-consumer"],
              "upstream": ["app-02"], "downstream": []},
    "backup-01": {"role": "backup", "env": "prod", "region": "cn-hangzhou", "owner": "运维组",
                  "ip": "10.12.3.61", "services": ["restic", "cron"],
                  "upstream": ["db-01"], "downstream": []},
}

SERVICES = {
    "web-01": [{"name": "nginx", "port": 80, "status": "up", "uptime_h": 1820, "restarts_24h": 0},
               {"name": "app-order", "port": 8081, "status": "up", "uptime_h": 410, "restarts_24h": 1}],
    "web-02": [{"name": "nginx", "port": 80, "status": "up", "uptime_h": 2210, "restarts_24h": 0},
               {"name": "app-user", "port": 8082, "status": "up", "uptime_h": 2210, "restarts_24h": 0}],
    "app-01": [{"name": "app-order", "port": 8081, "status": "up", "uptime_h": 96, "restarts_24h": 2},
               {"name": "app-user", "port": 8082, "status": "up", "uptime_h": 96, "restarts_24h": 0}],
    "app-02": [{"name": "app-pay", "port": 8083, "status": "up", "uptime_h": 12, "restarts_24h": 5}],
    "db-01": [{"name": "mysqld", "port": 3306, "status": "up", "uptime_h": 6, "restarts_24h": 3},
              {"name": "mysqld-exporter", "port": 9104, "status": "down", "uptime_h": 0, "restarts_24h": 0}],
    "cache-01": [{"name": "redis-server", "port": 6379, "status": "up", "uptime_h": 4300, "restarts_24h": 0}],
    "mq-01": [{"name": "beam-sasl", "port": 5672, "status": "up", "uptime_h": 640, "restarts_24h": 0},
              {"name": "mq-consumer", "port": 0, "status": "up", "uptime_h": 640, "restarts_24h": 1}],
    "backup-01": [{"name": "restic", "port": 0, "status": "up", "uptime_h": 30, "restarts_24h": 0},
                  {"name": "cron", "port": 0, "status": "up", "uptime_h": 7200, "restarts_24h": 0}],
}

CONFIGS = {
    "web-01": {"nginx": {"worker_connections": 512, "keepalive": 0, "log_level": "DEBUG",
                         "cert_expire_days": 24, "backup_enabled": True},
               "app-order": {"jdbc_pool_max": 20, "read_timeout_ms": 3000, "retry": 2,
                             "log_level": "INFO", "backup_enabled": True}},
    "web-02": {"nginx": {"worker_connections": 2048, "keepalive": 32, "log_level": "WARN",
                         "cert_expire_days": 210, "backup_enabled": True}},
    "app-01": {"app-order": {"jdbc_pool_max": 16, "read_timeout_ms": 3000, "retry": 1,
                             "log_level": "INFO", "backup_enabled": True},
               "app-user": {"jdbc_pool_max": 32, "read_timeout_ms": 2000, "retry": 2,
                            "log_level": "INFO", "backup_enabled": True}},
    "app-02": {"app-pay": {"jdbc_pool_max": 12, "read_timeout_ms": 9000, "retry": 0,
                           "log_level": "INFO", "backup_enabled": True}},
    "db-01": {"mysqld": {"jdbc_pool_max": 8, "read_timeout_ms": 12000, "retry": 0,
                         "log_level": "INFO", "backup_enabled": True, "cert_expire_days": 240}},
    "cache-01": {"redis-server": {"keepalive": 60, "log_level": "NOTICE", "backup_enabled": True,
                                  "cert_expire_days": 300}},
    "mq-01": {"beam-sasl": {"read_timeout_ms": 5000, "retry": 3, "log_level": "INFO",
                            "backup_enabled": True, "cert_expire_days": 150}},
    "backup-01": {"restic": {"backup_enabled": False, "cert_expire_days": 18, "retry": 1,
                             "log_level": "INFO"}},
}

LOG_TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    "web-01": [
        ("ERROR", "app-order", "disk space low: /var/log write failed errno=28 No space left on device"),
        ("WARN", "nginx", "logrotate skipped: /var/log/nginx/access.log size 7.4G exceeds threshold"),
        ("INFO", "nginx", "GET /api/order 200 12ms upstream=app-01"),
    ],
    "db-01": [
        ("FATAL", "mysqld", "Out of memory: Killed process 21733 (mysqld) total-vm 12884901kB"),
        ("ERROR", "app-order", "cannot get connection from connection pool, wait 5000ms timed out"),
        ("ERROR", "mysqld", "Too many connections (max_connections=100)"),
        ("WARN", "mysqld", "slow query 8.4s: SELECT * FROM order_item WHERE status=?"),
    ],
    "app-02": [
        ("ERROR", "app-pay", "read timed out after 9000ms calling http://mq-01:5672 enqueue"),
        ("WARN", "app-pay", "thread pool queue depth 480, request latency p99 11s"),
        ("ERROR", "app-pay", "upstream timeout while posting payment, retry disabled"),
    ],
    "mq-01": [
        ("WARN", "beam-sasl", "queue payment.retry length 18432 growing"),
        ("ERROR", "mq-consumer", "consumer stalled, prefetch exceeded 512 unacked messages"),
    ],
    "backup-01": [
        ("ERROR", "restic", "backup aborted: repository lock held by pid 8842"),
        ("WARN", "cron", "TLS certificate for backup.internal expires in 18 days"),
    ],
    "cache-01": [("INFO", "redis-server", "commands=120k rejected_connections=0 keyspace_hits 94.2%")],
    "app-01": [("INFO", "app-order", "order created id=%d latency=%dms"),
               ("WARN", "app-user", "connection pool near limit 15/16")],
    "web-02": [("INFO", "nginx", "GET /api/user 200 9ms upstream=app-01")],
}

HISTORY = [
    {"date": "2026-09-28", "host": "web-01", "symptom": "接口 5xx 突增", "root_cause": "磁盘写满导致日志无法落盘",
     "fix": "清理历史日志并开启 logrotate", "mttr_min": 34, "recurrence": 3},
    {"date": "2026-08-14", "host": "db-01", "symptom": "连接被拒", "root_cause": "连接池上限过低且应用未重试",
     "fix": "max_connections 上调 + 应用侧加退避重试", "mttr_min": 52, "recurrence": 2},
    {"date": "2026-07-02", "host": "db-01", "symptom": "mysqld 重启", "root_cause": "内存不足被 OOM Killer 终止",
     "fix": "限制慢查询并加内存，swap 兜底", "mttr_min": 41, "recurrence": 2},
    {"date": "2026-06-19", "host": "app-02", "symptom": "支付延迟上升", "root_cause": "MQ 消费停滞导致请求堆积",
     "fix": "扩容消费者并下调 prefetch", "mttr_min": 28, "recurrence": 1},
    {"date": "2026-05-30", "host": "backup-01", "symptom": "备份缺失告警", "root_cause": "备份任务开关被关闭",
     "fix": "恢复开关并加备份成功率监控", "mttr_min": 15, "recurrence": 1},
    {"date": "2026-05-11", "host": "web-01", "symptom": "HTTPS 握手失败", "root_cause": "证书到期未续",
     "fix": "接入自动续期并设 30 天告警", "mttr_min": 22, "recurrence": 2},
    {"date": "2026-04-22", "host": "app-02", "symptom": "CPU 长期满载", "root_cause": "正则回溯导致单线程打满",
     "fix": "改写正则并加超时保护", "mttr_min": 63, "recurrence": 1},
    {"date": "2026-04-03", "host": "cache-01", "symptom": "命中率下降", "root_cause": "缓存键版本不一致",
     "fix": "统一键前缀与过期策略", "mttr_min": 19, "recurrence": 1},
]

RUNBOOKS = {
    "rb-disk-full": {
        "title": "磁盘水位过高处置手册", "symptom": "磁盘使用率超过告警线、写入报错 errno=28、日志无法落盘",
        "cause": "日志未轮转、临时文件堆积、大对象直接落盘", "severity": "P1", "tags": "磁盘 容量 logrotate 清理",
        "steps": ["用 du -x --max-depth=1 定位增长目录，确认是日志还是业务文件在涨",
                  "先保业务：把已确认的历史日志压缩归档到备份卷，不要直接 rm",
                  "为对应日志开启 logrotate（按天滚动 + 保留 14 天 + 压缩）",
                  "确认为什么没开轮转：是配置漂移还是新服务上线漏了，记进巡检项",
                  "水位回落到 70% 以下后观察 2 小时，确认增长速率回到基线",
                  "若 7 天内再次触顶，走容量评估而不是重复清理"],
    },
    "rb-oom-kill": {
        "title": "进程被 OOM Killer 终止处置手册", "symptom": "服务无故重启、dmesg 出现 Killed process、内存水位持续高位",
        "cause": "内存泄漏、慢查询占用、实例内存规格不足", "severity": "P0", "tags": "内存 OOM 重启 mysql",
        "steps": ["先抓现场：dmesg 与 /proc/meminfo，确认被杀进程与当时 RSS",
                  "判断是尖峰还是趋势：看 24 小时内存曲线是否单调上涨（泄漏）还是抖动（突发请求）",
                  "尖峰型：限流或降级入口流量，避免重启风暴叠加",
                  "趋势型：导出堆快照后再重启，否则现场丢失无法定位",
                  "重启后确认服务就绪与依赖连接恢复，不确认就宣布恢复是常见错误",
                  "把根因写进工单并指定复验时间（48 小时内看曲线是否复现）"],
    },
    "rb-db-pool": {
        "title": "数据库连接池耗尽处置手册", "symptom": "cannot get connection、Too many connections、请求排队",
        "cause": "连接上限过低、事务未及时提交、慢查询长期占用连接", "severity": "P0", "tags": "连接池 数据库 超时 慢查询",
        "steps": ["区分是服务端连接数打满还是应用池打满：Too many connections 属前者",
                  "先看慢查询而不是先调大池——调大池只会把排队挪到数据库里",
                  "临时措施：下调单实例池上限并加队列超时，避免雪崩扩散",
                  "确认应用侧是否有未关闭连接（泄漏表现为池占用只增不减）",
                  "为关键接口加熔断与快速失败，retry=0 且无超时的组合必须整改",
                  "整改后压测验证 p99 与错误率，写回容量基线"],
    },
    "rb-cpu-saturation": {
        "title": "CPU 饱和处置手册", "symptom": "CPU 持续高位、load 超过核数、请求 p99 上升",
        "cause": "热点计算、正则回溯、GC 抖动、流量突增", "severity": "P1", "tags": "CPU 负载 热点 GC",
        "steps": ["用 top -H 找到线程，再看是否集中在少数线程（热点计算）还是均匀分布（流量）",
                  "采 30 秒火焰图，确认是业务代码还是 GC/序列化开销",
                  "流量型：先扩容或限流保响应，再优化",
                  "计算型：给正则与外部调用加超时，避免单请求拖垮工作线程",
                  "确认是否有重试放大（无退避的重试会把 CPU 打成雪崩）",
                  "优化后对比同窗口的 p99 与 CPU，避免只看均值"],
    },
    "rb-memory-pressure": {
        "title": "内存压力处置手册", "symptom": "内存水位持续 85% 以上、swap 使用上升、GC 变频繁",
        "cause": "缓存无上限、大结果集一次性加载、泄漏", "severity": "P1", "tags": "内存 缓存 泄漏 swap",
        "steps": ["确认是应用堆内存还是页缓存：页缓存高但可回收不算问题",
                  "给本地缓存加容量上限与淘汰策略（无上限的缓存是定时炸弹）",
                  "检查是否有全表扫描式加载，改为分页或流式",
                  "观察 GC 日志：老年代持续增长说明泄漏嫌疑大",
                  "设置内存告警在 80%，而不是等到 OOM 才被发现"],
    },
    "rb-cert-expiry": {
        "title": "证书临近过期处置手册", "symptom": "TLS 握手失败告警、证书剩余有效期小于 30 天",
        "cause": "自动续期任务失效、内网证书未纳入监控", "severity": "P1", "tags": "证书 TLS 到期 续期",
        "steps": ["核对证书链与域名，确认真正到期的是叶子证书还是中间证书",
                  "先验证自动续期为什么没生效（定时任务挂了比证书过期本身更值得修）",
                  "手动续期前先备份旧证书与私钥，出问题可回滚",
                  "续期后重启依赖服务并复验握手（只换文件不重启是常见失误）",
                  "把剩余有效期纳入巡检项，阈值 30 天，且备份成功率一并监控"],
    },
    "rb-downstream-timeout": {
        "title": "下游超时与请求堆积处置手册", "symptom": "read timed out、线程池队列变深、p99 上升但下游指标正常",
        "cause": "超时设置过长、无重试退避、下游容量不足", "severity": "P1", "tags": "超时 雪崩 重试 熔断",
        "steps": ["画出调用链，确认超时发生在哪一跳，不要只看入口错误率",
                  "把 9000ms 这类过长读超时下调到业务可接受值（通常 1~3s）",
                  "retry=0 意味着瞬时抖动直接变用户失败；加退避重试并设上限",
                  "为不可靠依赖加熔断与降级路径，避免线程被慢调用占满",
                  "确认队列深度告警是否早于错误率告警（前者更早）"],
    },
    "rb-mq-backlog": {
        "title": "消息队列堆积处置手册", "symptom": "队列长度持续增长、unacked 超过 prefetch、消费延迟上升",
        "cause": "消费者异常或容量不足、单条消息处理过慢、死信未处理", "severity": "P1", "tags": "MQ 堆积 消费 prefetch",
        "steps": ["先看消费者是否还活着，再看是不是处理速率跟不上产生速率",
                  "unacked 持续接近 prefetch 说明单条处理过慢，不是并发不够",
                  "临时扩容消费者实例，并确认消息处理是幂等的（扩容会导致重复消费）",
                  "把积压消息分流到独立队列追赶，而不是等它自然消化",
                  "复盘产生速率为什么上升：上游重试放大是最常见原因"],
    },
}


def _fmt(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def build_metrics(rng: random.Random) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for host in HOSTS:
        cpu0 = rng.uniform(18, 42)
        mem0 = rng.uniform(45, 66)
        disk0 = {"web-01": 62.0, "db-01": 58.0}.get(host, rng.uniform(30, 55))
        for h in range(HOURS):
            ts = BASE_TIME + dt.timedelta(hours=h)
            day = ts.hour / 24.0
            diurnal = 6 * (0.5 - abs(day - 0.5))  # 业务高峰在中午
            cpu = cpu0 + diurnal + rng.uniform(-4, 4)
            mem = mem0 + diurnal * 0.4 + rng.uniform(-3, 3)
            disk = disk0 + rng.uniform(-0.2, 0.2)
            if host == "web-01":                       # 注入：单调上涨（可外推）
                disk = 62.0 + 0.43 * h + rng.uniform(-0.25, 0.25)
            if host == "app-02":                       # 注入：CPU 饱和
                cpu = 86.0 + rng.uniform(-6, 6) if h > 24 else cpu0 + rng.uniform(-4, 4)
                cpu += 1.0
            if host == "db-01":                        # 注入：内存高位（后段 OOM）
                mem = 91.0 + rng.uniform(-2.5, 2.5) if h > 30 else mem
                if 55 <= h <= 60:
                    mem = 96.5 + rng.uniform(-1, 1)
            if host == "cache-01":                     # 阴性对照：全程健康
                cpu, mem, disk = 22.0 + rng.uniform(-5, 5), 40.0 + rng.uniform(-4, 4), 28.0 + rng.uniform(-1, 1)
            rows.append({
                "ts": _fmt(ts), "host": host,
                "cpu": round(max(1.0, min(cpu, 99.5)), 2),
                "mem": round(max(1.0, min(mem, 99.5)), 2),
                "disk": round(max(1.0, min(disk, 99.9)), 2),
                "io_wait": round(rng.uniform(0.2, 4.5) + (6.0 if host == "db-01" and h > 50 else 0), 2),
                "load": round(max(0.1, cpu / 12 + rng.uniform(-0.4, 0.6)), 2),
                "net_in_mbps": round(rng.uniform(12, 90) + (40 if host.startswith("web") else 0), 1),
            })
    return rows


def build_logs(rng: random.Random) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    weights = {"web-01": 26, "db-01": 24, "app-02": 18, "mq-01": 12, "backup-01": 8,
               "cache-01": 3, "app-01": 6, "web-02": 3}
    for host, per_hour in weights.items():
        for h in range(HOURS):
            n = max(0, int(rng.gauss(per_hour / 6, per_hour / 12)))
            for _ in range(n):
                level, service, msg = rng.choice(LOG_TEMPLATES[host])
                if "%d" in msg:
                    msg = msg % (rng.randint(10000, 99999), rng.randint(3, 40))
                ts = BASE_TIME + dt.timedelta(hours=h, minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
                rows.append({"ts": _fmt(ts), "host": host, "service": service,
                             "level": level, "message": msg})
    rows.sort(key=lambda r: str(r["ts"]))
    return rows


def write_all(outdir: str) -> dict[str, int]:
    root = Path(outdir)
    (root / "runbooks").mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    hosts_payload = {"generated_by": "scripts/seed_demo.py", "seed": SEED,
                     "hours": HOURS, "note": "合成演示数据，非真实生产数据", "hosts": HOSTS}
    _dump(root / "hosts.json", json.dumps(hosts_payload, ensure_ascii=False, indent=2))
    _dump(root / "services.json", json.dumps(SERVICES, ensure_ascii=False, indent=2))
    _dump(root / "configs.json", json.dumps(CONFIGS, ensure_ascii=False, indent=2))
    _dump(root / "metrics.jsonl", "\n".join(json.dumps(r, ensure_ascii=False) for r in build_metrics(rng)))
    _dump(root / "logs.jsonl", "\n".join(json.dumps(r, ensure_ascii=False) for r in build_logs(rng)))
    _dump(root / "incidents_history.jsonl", "\n".join(json.dumps(r, ensure_ascii=False) for r in HISTORY))
    for rid, rb in RUNBOOKS.items():
        md = [
            "---", f"id: {rid}", f"title: {rb['title']}", f"symptom: {rb['symptom']}",
            f"cause: {rb['cause']}", f"severity: {rb['severity']}", f"tags: {rb['tags']}", "---", "",
            f"# {rb['title']}", "", "**适用症状**：" + rb["symptom"], "", "**常见成因**：" + rb["cause"], "",
            "**处置步骤**：", "",
        ]
        md += [f"{i}. {s}" for i, s in enumerate(rb["steps"], 1)]
        md += ["", f"**严重级**：{rb['severity']}", ""]
        _dump(root / "runbooks" / f"{rid}.md", "\n".join(md))
    return {"hosts": len(HOSTS), "metrics": HOURS * len(HOSTS), "logs": 0,
            "services": sum(len(v) for v in SERVICES.values()), "runbooks": len(RUNBOOKS),
            "history": len(HISTORY)}


def _dump(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main() -> None:
    outdir = os.environ.get("OPSPILOT_DATA_DIR", "data")
    counts = write_all(os.path.join(outdir, "ops"))
    logs_n = sum(1 for _ in open(os.path.join(outdir, "ops", "logs.jsonl"), encoding="utf-8"))
    print("演示数据已生成 ->", os.path.abspath(os.path.join(outdir, "ops")))
    print(f"  主机 {counts['hosts']} 台｜指标点 {counts['metrics']}｜日志 {logs_n} 行｜"
          f"服务 {counts['services']} 个｜手册 {counts['runbooks']} 篇｜历史故障 {counts['history']} 条")
    print("  注入故障：web-01 磁盘上涨 / db-01 OOM+连接池 / app-02 CPU 饱和 / mq-01 堆积 / backup-01 配置 P1")
    print("  阴性对照：cache-01 全程健康（Agent 不应无中生有）")


if __name__ == "__main__":
    main()
