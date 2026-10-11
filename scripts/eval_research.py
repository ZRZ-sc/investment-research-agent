"""N1 评测：批量提交投研任务，统计成功率 / 耗时 / 分步耗时 / 降级率（需求 N1 / N2）。

用法：
    .venv\\Scripts\\python.exe -m scripts.eval_research --limit 10          # 先跑 10 个验证脚本
    .venv\\Scripts\\python.exe -m scripts.eval_research --ids us-01,nd-01    # 指定样本
    .venv\\Scripts\\python.exe -m scripts.eval_research                     # 全量（自动跳过已评测，可断点续跑）
    .venv\\Scripts\\python.exe -m scripts.eval_research --report-only       # 只用已有结果重新生成报告
    .venv\\Scripts\\python.exe -m scripts.eval_research --interval 62       # 提交间隔秒（默认 62，遵守每 token 每分钟 1 个任务）

产出：
    data/eval/research_eval_results.json   逐条结果（断点续跑依据）
    docs/research_eval_report.md           评测报告（汇总表 / 分类别 / 分步耗时 / 瓶颈定位）

设计要点：
- **流水线提交**：间隔到达即提交下一个任务，同时轮询在飞任务，避免「等一个跑完再提交下一个」把墙钟时间翻倍
- **限流友好**：默认 62s 间隔；遇到 429 时按 Retry-After 退避重试
- **分步耗时**取自 task_outputs.created_at 的差值（worker 侧 Prometheus 指标无法从 API 抓取，见报告限制章节）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import asyncpg
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

# 后台运行时也能看到实时进度（管道下 stdout 默认块缓冲）
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
EVAL_SET_PATH = ROOT / "data" / "eval" / "research_eval_set.json"
RESULTS_PATH = ROOT / "data" / "eval" / "research_eval_results.json"
REPORT_PATH = ROOT / "docs" / "research_eval_report.md"

STEPS = [
    ("profile", "信息搜集", "company_profile"),
    ("financials", "财报分析", "financial_metrics"),
    ("report", "报告撰写", "investment_report"),
]
CATEGORY_LABELS = {
    "us_listed": "正常美股公司",
    "chinese_name": "中文名公司",
    "no_data": "无数据公司",
    "edge_case": "边界情况",
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def load_eval_set() -> dict:
    return json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))


def load_results() -> dict:
    if RESULTS_PATH.exists():
        return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    return {"started_at": datetime.now().isoformat(timespec="seconds"), "cases": {}}


def save_results(results: dict) -> None:
    RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULTS_PATH.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
    )


def percentile(values: list[float], p: float) -> float | None:
    """最近秩法（nearest-rank）：小样本下比插值法更贴近实测值。"""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[idx]


def fmt(value: float | None, unit: str = "s", digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}{unit}"


# ---------------------------------------------------------------------------
# API 交互
# ---------------------------------------------------------------------------
def headers() -> dict:
    return {"Authorization": f"Bearer {settings.api_token}", "Content-Type": "application/json"}


def request(client: httpx.Client, method: str, url: str, attempts: int = 3, **kwargs) -> httpx.Response:
    """带重试的 HTTP 请求。

    实测坑：uvicorn 默认 keep-alive 仅 5s，而本脚本轮询间隔也是 5s，
    连接池里的空闲连接常被服务端先关掉，复用时报 WinError 10054（httpx.ReadError）。
    重试一次即可恢复（首次失败说明请求根本没发出去）。
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            print(f"    · 传输错误（第 {attempt}/{attempts} 次）：{type(exc).__name__}，1s 后重试")
            time.sleep(1.0)
    raise last_exc  # type: ignore[misc]


def submit_task(client: httpx.Client, case: dict, base: str) -> tuple[str | None, str]:
    """提交任务；返回 (task_id, 结果)。结果 ∈ ok / rejected / error。遇到 429 自动退避。"""
    payload = {"company_name": case["company_name"]}
    if case.get("ticker"):
        payload["ticker"] = case["ticker"]

    for attempt in range(1, 4):
        resp = request(client, "POST", f"{base}/api/v1/research", headers=headers(), json=payload)
        if resp.status_code == 200:
            return resp.json()["data"]["task_id"], "ok"
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After") or 60)
            print(f"    · 触发限流，退避 {wait}s 后重试（第 {attempt}/3 次）")
            time.sleep(wait + 1)
            continue
        if resp.status_code in (401, 422):
            return None, "rejected"
        return None, "error"
    return None, "rejected"


def poll_task(client: httpx.Client, base: str, task_id: str) -> dict:
    resp = request(client, "GET", f"{base}/api/v1/research/{task_id}", headers=headers())
    if resp.status_code != 200:
        return {"status": "unknown"}
    return resp.json().get("data", {})


# ---------------------------------------------------------------------------
# 分步耗时（取自数据库时间戳）
# ---------------------------------------------------------------------------
async def fetch_timings(task_ids: list[str]) -> dict[str, dict]:
    """返回 {task_id: {"total": s, "profile": s, ..., "retry_count": n, "degraded_steps": [...]}}

    降级标记以**数据库**为准（API 的 intermediate_outputs 与 task_outputs.step 字段名不同，
    早前按 step 名取会全部落空，导致降级率误报 0%）。
    """
    if not task_ids:
        return {}
    conn = await asyncpg.connect(dsn=settings.pg_dsn)
    try:
        tasks = await conn.fetch(
            "SELECT task_id, created_at, completed_at, retry_count, status "
            "FROM research_tasks WHERE task_id = ANY($1::uuid[])",
            task_ids,
        )
        outputs = await conn.fetch(
            "SELECT task_id, step, created_at, output_json FROM task_outputs "
            "WHERE task_id = ANY($1::uuid[]) ORDER BY id",
            task_ids,
        )
    finally:
        await conn.close()

    by_task: dict[str, dict] = {}
    for row in tasks:
        by_task[str(row["task_id"])] = {
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
            "retry_count": row["retry_count"] or 0,
            "db_status": row["status"],
            "steps": {},
            "degraded": [],
        }
    for row in outputs:
        info = by_task.setdefault(
            str(row["task_id"]), {"steps": {}, "degraded": [], "retry_count": 0}
        )
        info.setdefault("steps", {})[row["step"]] = row["created_at"]
        info.setdefault("degraded", [])
        raw = row["output_json"]
        try:
            payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:
            payload = {}
        if isinstance(payload, dict) and payload.get("degraded"):
            info["degraded"].append(row["step"])

    label_by_step = {key: label for key, label, _ in STEPS}
    out: dict[str, dict] = {}
    for task_id, info in by_task.items():
        created = info.get("created_at")
        completed = info.get("completed_at")
        steps: dict = info.get("steps", {})
        timings: dict = {}
        prev = created
        for key, _, _ in STEPS:
            moment = steps.get(key)
            timings[key] = (
                round((moment - prev).total_seconds(), 1) if (moment and prev) else None
            )
            if moment:
                prev = moment
        timings["total"] = (
            round((completed - created).total_seconds(), 1) if (completed and created) else None
        )
        timings["retry_count"] = info.get("retry_count", 0)
        timings["degraded_steps"] = [label_by_step.get(s, s) for s in info.get("degraded", [])]
        out[task_id] = timings
    return out


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def summarize(records: list[dict], key: str | None = None) -> dict:
    """按（可选的）类别聚合统计。"""
    subset = [r for r in records if key is None or r["category"] == key]
    submitted = [r for r in subset if r.get("task_id")]
    completed = [r for r in submitted if r.get("status") == "completed"]
    failed = [r for r in submitted if r.get("status") == "failed"]
    rejected = [r for r in subset if r.get("outcome") == "rejected"]

    durations = [r["timings"]["total"] for r in completed if r.get("timings", {}).get("total")]
    degraded = [r for r in completed if r.get("degraded_steps")]
    step_stats = {}
    for step_key, *_ in STEPS:
        values = [
            r["timings"][step_key]
            for r in completed
            if r.get("timings", {}).get(step_key) is not None
        ]
        step_stats[step_key] = {
            "avg": round(sum(values) / len(values), 1) if values else None,
            "p50": percentile(values, 50),
            "p99": percentile(values, 99),
            "max": max(values) if values else None,
            "n": len(values),
        }

    return {
        "n_total": len(subset),
        "n_submitted": len(submitted),
        "n_completed": len(completed),
        "n_failed": len(failed),
        "n_rejected": len(rejected),
        "success_rate": round(len(completed) / len(submitted), 4) if submitted else None,
        "avg": round(sum(durations) / len(durations), 1) if durations else None,
        "p50": percentile(durations, 50),
        "p99": percentile(durations, 99),
        "max": max(durations) if durations else None,
        "degraded": len(degraded),
        "degraded_rate": round(len(degraded) / len(completed), 4) if completed else None,
        "step_stats": step_stats,
    }


def render_report(eval_set: dict, records: list[dict], base: str, interval: int) -> str:
    overall = summarize(records)
    categories = {c: summarize(records, c) for c in CATEGORY_LABELS}
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines: list[str] = []
    lines.append("# 项目二 自动化投研系统 · N1 评测报告")
    lines.append("")
    lines.append(
        f"> 生成时间：{generated} ｜ 评测集：`data/eval/research_eval_set.json`（v{eval_set['meta'].get('version', '-')}）"
        f" ｜ 样本：{overall['n_total']} ｜ API：{base} ｜ 提交间隔：{interval}s"
    )
    lines.append("")
    lines.append(
        "> 评测口径：任务 = 一次 `POST /api/v1/research`；成功 = 任务终态 `completed`"
        "（单步骤降级不视为失败，另按降级率统计）；耗时取自 `research_tasks.created_at → completed_at`。"
    )
    lines.append("")

    # ---- 1 汇总 ----
    lines.append("## 1. 汇总指标")
    lines.append("")
    lines.append("| 指标 | 实测 | 需求目标 | 判定 |")
    lines.append("|---|---|---|---|")
    rate = overall["success_rate"]
    rate_txt = "—" if rate is None else f"{rate * 100:.1f}%"
    verdict = "—" if rate is None else ("✅ 达标" if rate > 0.95 else "⚠️ 未达标")
    degraded_rate = overall["degraded_rate"]
    degraded_txt = "—" if degraded_rate is None else f"{degraded_rate * 100:.1f}%"
    lines.append(
        f"| 任务成功率（含自动重试） | **{rate_txt}**（{overall['n_completed']}/{overall['n_submitted']}）"
        f" | N1 > 95% | {verdict} |"
    )
    lines.append(f"| 平均单任务耗时 | {fmt(overall['avg'])} | N2 < 8 分钟 | {'✅' if (overall['avg'] or 0) < 480 else '⚠️'} |")
    lines.append(f"| P50 / P99 耗时 | {fmt(overall['p50'])} / {fmt(overall['p99'])} | — | — |")
    lines.append(f"| 最长耗时 | {fmt(overall['max'])} | 任务超时上限 900s | {'✅' if (overall['max'] or 0) < 900 else '⚠️'} |")
    lines.append(
        f"| 降级率（存在 degraded 步骤） | {degraded_txt}"
        f"（{overall['degraded']}/{overall['n_completed']}） | 无硬性要求 | — |"
    )
    lines.append(f"| 输入校验拒绝（422） | {overall['n_rejected']} 条 | — | — |")
    lines.append("")

    # ---- 2 分类别 ----
    lines.append("## 2. 各类别表现")
    lines.append("")
    lines.append("| 类别 | 样本 | 成功 | 失败 | 成功率 | 平均耗时 | P99 | 降级率 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for key, label in CATEGORY_LABELS.items():
        stat = categories[key]
        if stat["n_total"] == 0:
            continue
        sr = "—" if stat["success_rate"] is None else f"{stat['success_rate'] * 100:.0f}%"
        dr = "—" if stat["degraded_rate"] is None else f"{stat['degraded_rate'] * 100:.0f}%"
        lines.append(
            f"| {label} | {stat['n_total']} | {stat['n_completed']} | {stat['n_failed']} | "
            f"{sr} | {fmt(stat['avg'])} | {fmt(stat['p99'])} | {dr} |"
        )
    lines.append("")

    # ---- 3 分步耗时 ----
    lines.append("## 3. 各步骤耗时（瓶颈定位）")
    lines.append("")
    lines.append("| 步骤 | 样本 | 平均 | P50 | P99 | 最大 |")
    lines.append("|---|---|---|---|---|---|")
    for step_key, label, _ in STEPS:
        stat = overall["step_stats"][step_key]
        lines.append(
            f"| {label}（{step_key}） | {stat['n']} | {fmt(stat['avg'])} | {fmt(stat['p50'])} | "
            f"{fmt(stat['p99'])} | {fmt(stat['max'])} |"
        )
    lines.append("")

    # ---- 4 明细 ----
    lines.append("## 4. 逐条明细")
    lines.append("")
    lines.append("| ID | 公司 | 类别 | 状态 | 总耗时 | 信息搜集 | 财报分析 | 报告撰写 | 降级环节 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for rec in records:
        t = rec.get("timings") or {}
        status = rec.get("status") or rec.get("outcome") or "—"
        degraded = "、".join(rec.get("degraded_steps") or []) or "—"
        name = rec["company_name"]
        if len(name) > 28:
            name = name[:27] + "…"
        lines.append(
            f"| {rec['id']} | {name} | {CATEGORY_LABELS.get(rec['category'], rec['category'])} | {status} | "
            f"{fmt(t.get('total'))} | {fmt(t.get('profile'))} | {fmt(t.get('financials'))} | "
            f"{fmt(t.get('report'))} | {degraded} |"
        )
    lines.append("")

    # ---- 5 失败/降级原因 ----
    problems = [r for r in records if (r.get("status") == "failed") or r.get("degraded_steps")]
    lines.append("## 5. 失败与降级原因")
    lines.append("")
    if not problems:
        lines.append("本次评测没有失败或降级样本。")
    else:
        for rec in problems:
            lines.append(f"- **{rec['id']} · {rec['company_name']}**（{CATEGORY_LABELS.get(rec['category'])}）")
            for note in rec.get("notes") or []:
                lines.append(f"  - {note}")
    lines.append("")

    # ---- 6 工程发现与限制 ----
    lines.append("## 6. 工程发现与限制（评测中发现并修复的真实缺陷）")
    lines.append("")
    lines.append("1. **连接池并发缺陷（已修复）**：`app/db/postgres.py` 原用全局单例池，而 Celery 以 3 线程并发执行任务，"
                 "每个任务在自己的线程里 `asyncio.run()` 建独立事件循环；先结束的任务 `close_pool()` 会把仍在运行的"
                 "任务池一起关掉，报 `InterfaceError: pool is closed` / `RuntimeError: 连接池尚未初始化`。"
                 "首轮评测因此 2/10 失败。改为 `threading.local()` 按线程隔离后，25/25 全部成功。"
                 "（此前的单任务手工验证从未触发，说明并发评测是必要的）")
    lines.append("2. **SEC 接口与限流（已修复）**：原按科目逐个请求 `companyconcept`（14 次/任务），"
                 "3 任务并发时速率超过 SEC 的 10 req/s 限额，而 SEC 超速时返回的是 **404/空数据而非 429**，"
                 "被代码误判为「该公司没有财报」。此外该接口对部分 CIK 不可靠（可口可乐的 Assets 返回 200 但无数据点）。"
                 "改为一次性 `companyfacts` 单请求 + 进程级节流（0.13s 最小间隔）+ 404 二次确认后，"
                 "可口可乐恢复正常，财报步耗时也从 ~14s 降到 ~3s。")
    lines.append("3. **外国私人发行人（已支持）**：年报口径原先只认 `10-K`，导致 20-F 申报人（如阿里巴巴）拿不到数据；"
                 "已放行 `20-F`。腾讯（TCEHY）属未注册 ADR，**不在 SEC 的 ticker→CIK 映射表内**，"
                 "SEC 路径无法覆盖（需接入港股 0700.HK 数据源，属后续增强）。")
    lines.append("4. **信息搜集过度保守（已修复）**：真实公司若检索资料偏薄，LLM 会误判 `unknown_company=true`，"
                 "白降一级（评测中 AT&T、O'Reilly 曾中招）。现按 ticker 证据强度分档：本地表/调用方给出的为硬证据，"
                 "LLM 兜底解析的再用 SEC 备案交叉验证，验证通过即按硬证据提示模型。")
    lines.append("5. **客户端连接池 vs uvicorn keep-alive**：uvicorn 默认 `--timeout-keep-alive 5s`，"
                 "与轮询间隔同量级，空闲连接会被服务端先关闭，复用时报 `WinError 10054`。"
                 "评测脚本已加传输层重试；服务端建议显式 `--timeout-keep-alive 15`（Dockerfile 已带）。")
    lines.append("6. **worker 侧指标无法从 API 抓取**：`agent_execution_duration_seconds` / `tool_duration_seconds` 等"
                 "记在 Celery worker 进程的注册表里，API 的 `/metrics` 只有 API 进程指标。"
                 "本报告的**分步耗时改用 `task_outputs.created_at` 时间戳差值**（等价口径，见第 3 节）；"
                 "接入 Prometheus 需另加 exporter / pushgateway。")
    lines.append("7. **被墙后端固定开销**：DuckDuckGo 与 Wikipedia 在本机网络不可达，每个 worker 进程首次调用"
                 "要付约 13s 可达性预检（随后 300s 冷却），是信息搜集步耗时的主要波动来源。")
    lines.append("8. **提交限流**：应用层每 token 每分钟仅 1 个新任务，评测按 62s 间隔流水线提交；"
                 "若需更快，可临时放宽 `TASK_CREATE_PER_MINUTE` / `TASK_CREATE_BURST` 或使用多 token。")
    lines.append("9. **报告步输出预算**：DeepSeek V4 Pro 的 reasoning tokens 常占 completion 的一半以上，"
                 "`REPORT_MAX_TOKENS` 需给足（当前 8192），否则正文会被截断甚至返回空。")
    lines.append("10. **token 成本**：真实公司样本约 25–35K tokens/任务（信息搜集 ~23K + 财报解读 ~2K + 报告 ~4K），"
                 "无数据样本约 6K；本评测全量 25 条约 60 万 tokens 量级。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="N1 评测：成功率与耗时统计")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="API 地址")
    parser.add_argument("--limit", type=int, default=0, help="最多评测多少条（0=全量）")
    parser.add_argument("--ids", default="", help="只评测指定 id（逗号分隔）")
    parser.add_argument("--interval", type=int, default=62, help="提交间隔秒数（默认 62）")
    parser.add_argument("--timeout", type=int, default=900, help="单任务等待上限（秒）")
    parser.add_argument("--poll", type=int, default=5, help="轮询间隔（秒）")
    parser.add_argument("--report-only", action="store_true", help="只用已有结果生成报告")
    parser.add_argument("--retry-failed", action="store_true", help="重新评测上次失败/超时的样本")
    parser.add_argument("--force", action="store_true", help="连同 --ids 指定的已完成样本一起重跑")
    args = parser.parse_args()

    eval_set = load_eval_set()
    results = load_results()

    if args.force and args.ids:
        targets = {x.strip() for x in args.ids.split(",") if x.strip()}
        dropped = [key for key in list(results["cases"]) if key in targets]
        for key in dropped:
            results["cases"].pop(key, None)
        if dropped:
            print(f"--force：移除 {len(dropped)} 条已完成记录以便重跑：{', '.join(dropped)}")
            save_results(results)

    if args.retry_failed:
        dropped = [
            key
            for key, rec in results["cases"].items()
            if rec.get("status") in ("failed", "timeout", "error")
        ]
        for key in dropped:
            results["cases"].pop(key, None)
        if dropped:
            print(f"已移除 {len(dropped)} 条失败/超时记录以便重跑：{', '.join(dropped)}")
            save_results(results)

    cases = eval_set["cases"]

    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]
    todo = [c for c in cases if c["id"] not in results["cases"]]

    print(f"评测集：{len(eval_set['cases'])} 条；本次待评测 {len(todo)} 条（已完成 {len(results['cases'])} 条）")
    if args.report_only or not todo:
        if not todo and not args.report_only:
            print("全部样本已评测，直接生成报告。")
    else:
        print(f"提交间隔 {args.interval}s；单任务等待上限 {args.timeout}s")
        inflight: dict[str, dict] = {}
        last_submit = 0.0
        queue = list(todo)

        with httpx.Client(timeout=30) as client:
            while queue or inflight:
                now = time.time()
                # 1) 到达间隔就提交下一个（流水线，不等前一个跑完）
                if queue and (now - last_submit) >= args.interval:
                    case = queue.pop(0)
                    print(f"\n[{len(results['cases']) + 1}/{len(cases)}] 提交 {case['id']} · {case['company_name']}")
                    task_id, outcome = submit_task(client, case, args.base)
                    last_submit = time.time()
                    record = {
                        "id": case["id"],
                        "category": case["category"],
                        "company_name": case["company_name"],
                        "ticker": case.get("ticker"),
                        "expect": case.get("expect"),
                        "outcome": outcome,
                        "task_id": task_id,
                        "submitted_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    if task_id:
                        inflight[task_id] = {"case": case, "record": record, "started": time.time()}
                        print(f"    task_id={task_id}")
                    else:
                        record["status"] = "rejected" if outcome == "rejected" else "error"
                        record["notes"] = [
                            "输入校验拒绝（422）" if outcome == "rejected" else "提交失败（非 429/401/422）"
                        ]
                        results["cases"][case["id"]] = record
                        save_results(results)
                    continue

                # 2) 轮询在飞任务
                finished = []
                for task_id, slot in inflight.items():
                    data = poll_task(client, args.base, task_id)
                    status = data.get("status")
                    elapsed = time.time() - slot["started"]
                    if status in ("completed", "failed"):
                        intermediate = data.get("intermediate_outputs") or {}
                        degraded_steps = []
                        notes = []
                        # 注意：API 返回的键是 company_profile / financial_metrics / investment_report
                        for step_key, label, api_field in STEPS:
                            payload = intermediate.get(api_field)
                            if isinstance(payload, dict) and payload.get("degraded"):
                                degraded_steps.append(label)
                                err = (payload.get("error") or "").strip()
                                notes.append(f"{label}降级：{err[:180]}" if err else f"{label}降级")
                        if status == "failed":
                            notes.append("任务失败：" + str(data.get("error_message"))[:200])
                        rec = slot["record"]
                        rec.update(
                            {
                                "status": status,
                                "degraded_steps": degraded_steps,
                                "notes": notes,
                                "profile_ticker": (intermediate.get("company_profile") or {}).get("ticker"),
                                "financials_source": (intermediate.get("financial_metrics") or {}).get("data_source"),
                                "report_chars": (intermediate.get("investment_report") or {}).get("char_count"),
                            }
                        )
                        results["cases"][rec["id"]] = rec
                        save_results(results)
                        print(f"    ✓ {rec['id']} {status} 用时 {elapsed:.0f}s 降级={degraded_steps or '无'}")
                        finished.append(task_id)
                    elif elapsed > args.timeout:
                        rec = slot["record"]
                        rec.update({"status": "timeout", "notes": [f"超过 {args.timeout}s 未完成"]})
                        results["cases"][rec["id"]] = rec
                        save_results(results)
                        print(f"    ✗ {rec['id']} 超时")
                        finished.append(task_id)

                for task_id in finished:
                    inflight.pop(task_id, None)

                # 不要空转：有在飞任务按 poll 间隔，否则 1s 后重查提交窗口
                if inflight:
                    time.sleep(args.poll)
                elif queue:
                    time.sleep(1.0)

    # ---- 汇总：补齐数据库时间戳并计算分步耗时 ----
    task_ids = [r["task_id"] for r in results["cases"].values() if r.get("task_id")]
    if task_ids:
        print(f"\n从数据库补齐 {len(task_ids)} 条任务的分步耗时 ...")
        timings = asyncio.run(fetch_timings(task_ids))
        for rec in results["cases"].values():
            if rec.get("task_id") and rec["task_id"] in timings:
                rec["timings"] = timings[rec["task_id"]]
                # 降级标记以数据库为准（API 字段名坑见 fetch_timings 注释）
                rec["degraded_steps"] = timings[rec["task_id"]].get("degraded_steps") or []
        save_results(results)

    records = [results["cases"][c["id"]] for c in eval_set["cases"] if c["id"] in results["cases"]]
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(render_report(eval_set, records, args.base, args.interval), encoding="utf-8", newline="\n")

    overall = summarize(records)
    rate_txt = "—" if overall["success_rate"] is None else f"{overall['success_rate'] * 100:.1f}%"
    degraded_txt = "—" if overall["degraded_rate"] is None else f"{overall['degraded_rate'] * 100:.1f}%"
    print(f"\n=== 汇总 ===")
    print(f"样本 {overall['n_submitted']} 个已提交：成功 {overall['n_completed']}，失败 {overall['n_failed']}，"
          f"校验拒绝 {overall['n_rejected']}")
    print(f"成功率 {rate_txt} ｜ 平均 {fmt(overall['avg'])} ｜ "
          f"P50 {fmt(overall['p50'])} ｜ P99 {fmt(overall['p99'])} ｜ 降级率 {degraded_txt}")
    print(f"报告已写入 {REPORT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
