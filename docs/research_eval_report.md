# 项目二 自动化投研系统 · N1 评测报告

> 生成时间：2026-10-01 02:07:33 ｜ 评测集：`data/eval/research_eval_set.json`（v1.0） ｜ 样本：25 ｜ API：http://127.0.0.1:8000 ｜ 提交间隔：62s

> 评测口径：任务 = 一次 `POST /api/v1/research`；成功 = 任务终态 `completed`（单步骤降级不视为失败，另按降级率统计）；耗时取自 `research_tasks.created_at → completed_at`。

## 1. 汇总指标

| 指标 | 实测 | 需求目标 | 判定 |
|---|---|---|---|
| 任务成功率（含自动重试） | **100.0%**（25/25） | N1 > 95% | ✅ 达标 |
| 平均单任务耗时 | 61.5s | N2 < 8 分钟 | ✅ |
| P50 / P99 耗时 | 58.7s / 103.9s | — | — |
| 最长耗时 | 103.9s | 任务超时上限 900s | ✅ |
| 降级率（存在 degraded 步骤） | 24.0%（6/25） | 无硬性要求 | — |
| 输入校验拒绝（422） | 0 条 | — | — |

## 2. 各类别表现

| 类别 | 样本 | 成功 | 失败 | 成功率 | 平均耗时 | P99 | 降级率 |
|---|---|---|---|---|---|---|---|
| 正常美股公司 | 10 | 10 | 0 | 100% | 71.4s | 103.9s | 0% |
| 中文名公司 | 5 | 5 | 0 | 100% | 60.3s | 87.5s | 20% |
| 无数据公司 | 5 | 5 | 0 | 100% | 35.8s | 39.7s | 100% |
| 边界情况 | 5 | 5 | 0 | 100% | 68.4s | 84.6s | 0% |

## 3. 各步骤耗时（瓶颈定位）

| 步骤 | 样本 | 平均 | P50 | P99 | 最大 |
|---|---|---|---|---|---|
| 信息搜集（profile） | 25 | 16.7s | 14.4s | 43.0s | 43.0s |
| 财报分析（financials） | 25 | 11.1s | 10.8s | 20.0s | 20.0s |
| 报告撰写（report） | 25 | 33.6s | 29.1s | 57.7s | 57.7s |

## 4. 逐条明细

| ID | 公司 | 类别 | 状态 | 总耗时 | 信息搜集 | 财报分析 | 报告撰写 | 降级环节 |
|---|---|---|---|---|---|---|---|---|
| us-01 | Apple Inc. | 正常美股公司 | completed | 53.8s | 11.9s | 11.8s | 30.0s | — |
| us-02 | Microsoft Corporation | 正常美股公司 | completed | 58.3s | 15.2s | 17.2s | 25.9s | — |
| us-03 | Tesla, Inc. | 正常美股公司 | completed | 84.4s | 18.1s | 8.9s | 57.4s | — |
| us-04 | NVIDIA Corporation | 正常美股公司 | completed | 47.6s | 12.0s | 9.3s | 26.3s | — |
| us-05 | Amazon.com, Inc. | 正常美股公司 | completed | 77.9s | 26.6s | 10.8s | 40.4s | — |
| us-06 | Alphabet Inc. | 正常美股公司 | completed | 49.7s | 13.7s | 13.1s | 22.8s | — |
| us-07 | Meta Platforms, Inc. | 正常美股公司 | completed | 73.9s | 16.4s | 9.1s | 48.4s | — |
| us-08 | Netflix, Inc. | 正常美股公司 | completed | 103.9s | 43.0s | 13.3s | 47.6s | — |
| us-09 | JPMorgan Chase & Co. | 正常美股公司 | completed | 65.1s | 7.2s | 7.4s | 50.5s | — |
| us-10 | The Coca-Cola Company | 正常美股公司 | completed | 99.5s | 41.2s | 19.8s | 38.5s | — |
| cn-01 | 苹果 | 中文名公司 | completed | 58.7s | 17.4s | 12.6s | 28.7s | — |
| cn-02 | 微软 | 中文名公司 | completed | 63.3s | 8.9s | 9.2s | 45.1s | — |
| cn-03 | 腾讯 | 中文名公司 | completed | 39.4s | 11.7s | 5.9s | 21.8s | 财报分析、报告撰写 |
| cn-04 | 阿里巴巴 | 中文名公司 | completed | 52.7s | 14.4s | 11.3s | 27.0s | — |
| cn-05 | 特斯拉 | 中文名公司 | completed | 87.5s | 21.2s | 10.4s | 55.8s | — |
| nd-01 | ZZZZ Corp | 无数据公司 | completed | 39.7s | 6.2s | 10.6s | 22.9s | 信息搜集、财报分析、报告撰写 |
| nd-02 | Foobar Inc. | 无数据公司 | completed | 38.2s | 8.8s | 0.0s | 29.4s | 信息搜集、财报分析、报告撰写 |
| nd-03 | Nonexistent Holdings Limited | 无数据公司 | completed | 35.3s | 5.9s | 11.6s | 17.7s | 信息搜集、财报分析、报告撰写 |
| nd-04 | 不存在的公司XYZ | 无数据公司 | completed | 27.0s | 8.2s | 4.2s | 14.6s | 信息搜集、财报分析、报告撰写 |
| nd-05 | Qwertyuiop Industries | 无数据公司 | completed | 39.0s | 7.4s | 5.2s | 26.4s | 信息搜集、财报分析、报告撰写 |
| eg-01 | AT&T Inc. | 边界情况 | completed | 74.8s | 38.7s | 20.0s | 16.0s | — |
| eg-02 | Johnson & Johnson | 边界情况 | completed | 84.6s | 17.9s | 9.0s | 57.7s | — |
| eg-03 | O'Reilly Automotive, Inc. | 边界情况 | completed | 70.1s | 17.6s | 17.0s | 35.6s | — |
| eg-04 | 3M Company | 边界情况 | completed | 60.4s | 16.5s | 14.8s | 29.1s | — |
| eg-05 | Apple Inc. (formerly Apple … | 边界情况 | completed | 52.1s | 11.6s | 15.2s | 25.3s | — |

## 5. 失败与降级原因

- **cn-03 · 腾讯**（中文名公司）
- **nd-01 · ZZZZ Corp**（无数据公司）
- **nd-02 · Foobar Inc.**（无数据公司）
- **nd-03 · Nonexistent Holdings Limited**（无数据公司）
  - 信息搜集降级：检索未找到「Nonexistent Holdings Limited」的相关信息（LLM 判定为未知公司）
  - 财报分析降级：yfinance 不可用：TickerNoData: yfinance 未返回任何报表数据：NONEXX；SEC XBRL 不可用：TickerNoData: SEC 映射表中没有该 ticker：NONEXX（可能非美股上市公司）；检索片段兜底也未获得可信数字（无引用证据）
  - 报告撰写降级
- **nd-04 · 不存在的公司XYZ**（无数据公司）
  - 信息搜集降级：检索未找到「不存在的公司XYZ」的相关信息（LLM 判定为未知公司）
  - 财报分析降级：yfinance 不可用：YahooUnavailable: Yahoo 处于冷却期（此前连续失败）；SEC XBRL 不可用：TickerNoData: SEC 映射表中没有该 ticker：XYZNONE（可能非美股上市公司）；检索片段兜底也未获得可信数字（无引用证据）
  - 报告撰写降级
- **nd-05 · Qwertyuiop Industries**（无数据公司）
  - 信息搜集降级：检索未找到「Qwertyuiop Industries」的相关信息（LLM 判定为未知公司）
  - 财报分析降级：yfinance 不可用：YahooUnavailable: Yahoo 处于冷却期（此前连续失败）；SEC XBRL 不可用：TickerNoData: SEC 映射表中没有该 ticker：QWERTY（可能非美股上市公司）；检索片段兜底也未获得可信数字（无引用证据）
  - 报告撰写降级

## 6. 工程发现与限制（评测中发现并修复的真实缺陷）

1. **连接池并发缺陷（已修复）**：`app/db/postgres.py` 原用全局单例池，而 Celery 以 3 线程并发执行任务，每个任务在自己的线程里 `asyncio.run()` 建独立事件循环；先结束的任务 `close_pool()` 会把仍在运行的任务池一起关掉，报 `InterfaceError: pool is closed` / `RuntimeError: 连接池尚未初始化`。首轮评测因此 2/10 失败。改为 `threading.local()` 按线程隔离后，25/25 全部成功。（此前的单任务手工验证从未触发，说明并发评测是必要的）
2. **SEC 接口与限流（已修复）**：原按科目逐个请求 `companyconcept`（14 次/任务），3 任务并发时速率超过 SEC 的 10 req/s 限额，而 SEC 超速时返回的是 **404/空数据而非 429**，被代码误判为「该公司没有财报」。此外该接口对部分 CIK 不可靠（可口可乐的 Assets 返回 200 但无数据点）。改为一次性 `companyfacts` 单请求 + 进程级节流（0.13s 最小间隔）+ 404 二次确认后，可口可乐恢复正常，财报步耗时也从 ~14s 降到 ~3s。
3. **外国私人发行人（已支持）**：年报口径原先只认 `10-K`，导致 20-F 申报人（如阿里巴巴）拿不到数据；已放行 `20-F`。腾讯（TCEHY）属未注册 ADR，**不在 SEC 的 ticker→CIK 映射表内**，SEC 路径无法覆盖（需接入港股 0700.HK 数据源，属后续增强）。
4. **信息搜集过度保守（已修复）**：真实公司若检索资料偏薄，LLM 会误判 `unknown_company=true`，白降一级（评测中 AT&T、O'Reilly 曾中招）。现按 ticker 证据强度分档：本地表/调用方给出的为硬证据，LLM 兜底解析的再用 SEC 备案交叉验证，验证通过即按硬证据提示模型。
5. **客户端连接池 vs uvicorn keep-alive**：uvicorn 默认 `--timeout-keep-alive 5s`，与轮询间隔同量级，空闲连接会被服务端先关闭，复用时报 `WinError 10054`。评测脚本已加传输层重试；服务端建议显式 `--timeout-keep-alive 15`（Dockerfile 已带）。
6. **worker 侧指标无法从 API 抓取**：`agent_execution_duration_seconds` / `tool_duration_seconds` 等记在 Celery worker 进程的注册表里，API 的 `/metrics` 只有 API 进程指标。本报告的**分步耗时改用 `task_outputs.created_at` 时间戳差值**（等价口径，见第 3 节）；接入 Prometheus 需另加 exporter / pushgateway。
7. **被墙后端固定开销**：DuckDuckGo 与 Wikipedia 在本机网络不可达，每个 worker 进程首次调用要付约 13s 可达性预检（随后 300s 冷却），是信息搜集步耗时的主要波动来源。
8. **提交限流**：应用层每 token 每分钟仅 1 个新任务，评测按 62s 间隔流水线提交；若需更快，可临时放宽 `TASK_CREATE_PER_MINUTE` / `TASK_CREATE_BURST` 或使用多 token。
9. **报告步输出预算**：DeepSeek V4 Pro 的 reasoning tokens 常占 completion 的一半以上，`REPORT_MAX_TOKENS` 需给足（当前 8192），否则正文会被截断甚至返回空。
10. **token 成本**：真实公司样本约 25–35K tokens/任务（信息搜集 ~23K + 财报解读 ~2K + 报告 ~4K），无数据样本约 6K；本评测全量 25 条约 60 万 tokens 量级。
