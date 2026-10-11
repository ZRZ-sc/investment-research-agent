# 环境适配与事故复盘（本机开发环境）

> 记录时间：2026-09-29 ~ 10-01 ｜ 适用范围：本机两个项目（项目一 RAG 智能客服、项目二 Agent 投研系统）共用的开发环境
>
> 本文档两部分：**第 1–4 节**是项目一期间的一次真实事故复盘（C 盘写满导致 WSL 根文件系统只读）；
> **第 5 节**是项目二落地过程中确认的环境适配清单（网络受限、端口共存、外部接口行为等）。

## 0. 一句话结论

本机环境有三个硬约束：**Docker 只能跑在 WSL 里**、**docker.io / HuggingFace / Yahoo / DuckDuckGo / Wikipedia 等外部依赖不可直连**、
**磁盘与 WSL 生命周期需要人为保活**。两个项目的代码都按"外部依赖不可靠"来设计降级链，因此这些约束没有变成功能性缺陷，只影响耗时与数据源优先级。

## 1. 现象

| 表现 | 证据 |
|---|---|
| Docker 镜像构建在最后一层失败 | `error committing ...: write /var/lib/docker/buildkit/containerd-overlayfs/metadata_v2.db: read-only file system` |
| `pip` 装完包后大量告警 | `WARNING: Failed to remove contents in a temporary directory '/tmp/pip-...'` |
| 问答日志/工单/反馈写库失败 | `dialogue log write failed: could not open file "base/16384/2601": Read-only file system` |
| 限流降级 | `rate limit check failed (fail-open): MISCONF Errors writing to the AOF file: Read-only file system` |
| 管理后台日志接口 500 | `asyncpg.exceptions.InsufficientPrivilegeError: could not open file "base/16384/2601": Read-only file system` |
| WSL 内任何写入失败 | `touch /root/x` → `Read-only file system`；`mount` 显示 `(rw,...,emergency_ro)` |

值得注意：**读操作仍然正常**（L1 缓存命中、Milvus 检索、RAG 生成都成功），因此表面看"服务是好的"，
只有写入路径（PG 落库、Redis AOF、镜像层提交）在静默失败——这正是本项目各项降级/兜底逻辑生效的地方：
对话仍能返回答案，同时日志里留下明确告警，而不是整站不可用。

## 2. 根因分析

```
C 盘（98.9GB）剩余 0GB
   └─ Ubuntu WSL 虚拟磁盘 ext4.vhdx（7.71GB）位于
      C:\Users\<user>\AppData\Local\wsl\{GUID}\
        └─ 磁盘写满 → ext4 触发错误保护 → 根文件系统 remount read-only（emergency_ro）
             ├─ /var/lib/docker 不可写 → buildkit 无法提交镜像层
             ├─ PostgreSQL 数据文件不可写 → INSERT/UPDATE 报 EROFS
             └─ Redis AOF 重写失败 → 限流/缓存写入报 MISCONF
```

排查要点（可直接复用的命令）：

```powershell
Get-Volume | Where-Object DriveLetter | Select-Object DriveLetter, SizeRemaining   # 看 C 盘是否写满
wsl -d Ubuntu -u root -e bash -c "mount | grep ' / '"                               # 看是否出现 emergency_ro
wsl -d Ubuntu -u root -e bash -c "touch /root/x && echo ok"                         # 写测试
Get-ChildItem "HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss" | ForEach-Object { Get-ItemProperty $_.PSPath }  # 发行版磁盘位置
```

## 3. 处置过程

1. **腾空间前的干扰项**：`wsl --shutdown` 后发行版立刻又被拉起，`wsl --manage --move` 报
   `WSL_E_DISTRO_NOT_STOPPED`。原因是两个常驻进程：
   - Docker Desktop（`Docker Desktop.exe` / `com.docker.backend`）会维持 WSL 集成发行版运行；
   - `scripts\infra_up.ps1 -Keepalive` 的保活窗口持有 `wsl -d Ubuntu ... "while true; do sleep 20; done"` 会话。
2. 退出 Docker Desktop 与保活窗口 → `wsl --shutdown` → 确认 `Ubuntu = Stopped`。
3. **迁移发行版磁盘到 D 盘**（原生指令，秒级完成，不复制数据、不丢数据）：

   ```powershell
   wsl --manage Ubuntu --move D:\WSL\Ubuntu
   ```

4. 重启后校验：`mount` 不再有 `emergency_ro`、写测试通过、`docker ps` 显示 7 个容器
   （`restart: unless-stopped`）自动恢复 healthy。

### 效果

| 指标 | 迁移前 | 迁移后 |
|---|---|---|
| C 盘可用 | ≈ 0 GB | **10.68 GB** |
| D 盘可用 | 86.5 GB | 78.78 GB（`D:\WSL\Ubuntu\ext4.vhdx` 7.76GB） |
| 根文件系统 | `rw,...,emergency_ro`（只读） | `rw,relatime,...,errors=remount-ro`（可写） |
| 写库验证 | `dialogue log write failed ... Read-only file system` | 新日志行 `id=157724` 正常落库 |
| 镜像构建 | 最后一层提交失败 | 见 `docs/production_plan.md` 同批次构建结论 |

## 4. 复现与预防建议

- **保持 C 盘余量 ≥ 10GB**：WSL2 的 `ext4.vhdx`、Docker 层、构建缓存都会写系统盘。
- **发行版磁盘放数据盘**：新装/迁移时用 `wsl --manage <distro> --move <path>`，避免系统盘被撑满。
- **构建前先探活**：`wsl -d Ubuntu -u root -e bash -c "touch /var/lib/docker/.t && rm /var/lib/docker/.t"`，
  只读状态下直接改用 `wsl --shutdown` 重启（或先腾空间），不要在只读状态里反复重试构建。
- **国内网络构建**：torch 走 `https://mirror.sjtu.edu.cn/pytorch-wheels/cpu`、其余依赖走清华 PyPI
  （实测 3.8MB/s vs 官方源 0.85MB/s，官方源还会长时间无连接卡死）。
- **监控写入健康**：`/ready` 已检查 PG/Redis/Milvus/LLM，建议再叠加"最近 5 分钟 dialogue_logs 是否有新增"
  的告警（只读故障下读接口全绿、写入全红，单看探针会漏报）。
  > 项目二对应做法：`/ready` 检查 PostgreSQL + Redis + **Celery broker** 三项；写入健康可看
  > `research_tasks` 最近 5 分钟是否有新增，或 `research_tasks_in_progress` / 队列长度指标。

---

## 5. 项目二（Agent 投研系统）的环境适配清单

### 5.1 基础设施与网络

| 问题 | 现象 | 处理 |
|---|---|---|
| Docker Desktop 不可用 | 引擎起不来 | Docker 装在 **WSL Ubuntu** 内，用 `scripts\infra_up.ps1` 管理（脚本从自身位置推导项目路径，不会指错项目） |
| docker.io 被墙 | 拉镜像 `i/o timeout` | dockerd 配镜像加速；**优先复用已缓存镜像**（如 PostgreSQL 用已缓存的 `pgvector/pgvector:pg15`，不为了"纯净"去拉 `postgres:15-alpine`） |
| WSL VM 被宿主回收 | 无活跃会话约 1 分钟后容器停止 | `infra_up.ps1 -Keepalive` 前台保活 + 容器 `restart: unless-stopped` |
| 与项目一端口冲突 | 5432 / 6379 已被项目一容器占用 | 项目二改用 **5433 / 6380**（`.env.local` 覆盖层），容器内端口不变；compose 用 `--env-file .env --env-file .env.local` 读取 |
| pip 官方源极慢 | `pip install` 长时间无进展 | `-i https://pypi.tuna.tsinghua.edu.cn/simple`；`requirements.txt` 顶部已注明 |
| WSL 磁盘写满 | 见第 1–4 节事故复盘 | 发行版磁盘迁到数据盘 `wsl --manage Ubuntu --move D:\WSL\Ubuntu`；保持 C 盘余量 ≥10GB |

### 5.2 外部数据源（项目二的关键约束）

| 依赖 | 本机实测 | 项目二的应对 |
|---|---|---|
| Yahoo Finance（yfinance） | 直连 4 个端点全 **HTTP 403**；装了 yfinance 后 `YFRateLimitError`，三大报表返回空表 | 自动降级 **SEC XBRL**（本机可达）；换到能访问 Yahoo 的网络会自动恢复 yfinance 主源，代码无需改 |
| DuckDuckGo / Wikipedia | `ConnectTimeout`（被墙），且 ddgs 库内部走后端域名同样不通 | 3s 可达性预检 + 300s 冷却 → 降级 **Bing**（cn.bing.com 可达）；挂代理即恢复主源 |
| SEC EDGAR | **可达**；但对超速请求返回 404/空数据而非 429 | 进程级节流（0.13s）+ `companyfacts` 单请求 + 404 二次确认（细节见评测报告第 6 节） |
| 新浪 / 腾讯行情 | **可达**，免 Key；对不存在的 ticker 会返回占位数据 | 作报价降级源；加"价格/市值必须有效"校验，防占位数据被当成真行情 |
| DeepSeek API | **可达**（`/v1/models` 需鉴权，模型名 `deepseek-flash` / `deepseek-v4-pro`，`deepseek-v4-flash` 亦可用） | — |
| jsDelivr（marked.js） | 可达 | 仍把 `marked.min.js` **本地化**到 `app/static/vendor/`，演示不依赖 CDN（页面留 CDN 兜底） |

### 5.3 运行时与框架坑

| 问题 | 现象 | 处理 |
|---|---|---|
| CrewAI 首次运行弹交互提示 | `Share this execution trace with CrewAI?` 在 TTY 下阻塞 20s | `CREWAI_TRACING_ENABLED=false` + `OTEL_SDK_DISABLED=true` + `Crew(tracing=False)`（worker/容器必须非交互） |
| Windows Celery 线程池 | 默认 prefork 池在 Windows 不受支持 | worker 启动加 `-P threads`；注意**线程池不支持软超时**，硬超时仍有效（Linux/Docker 用默认池两者都生效） |
| uvicorn keep-alive 5s | 客户端轮询间隔同量级 → 空闲连接被服务端先关，`WinError 10054` | 服务端 `--timeout-keep-alive 15`（Dockerfile 已带）；客户端加传输层重试 |
| 中文日志/输出在某控制台乱码 | Windows 控制台默认 GBK | `setup_logging()` 强制 stdout/stderr UTF-8（项目一已修）；PowerShell 校验中文建议改用 Python/`curl.exe` |
| 受限沙箱无命名管道 | loguru `enqueue=True` 启动失败 | 不用异步日志队列（单进程无需） |

### 5.4 复现检查清单（换机时按序确认）

```powershell
wsl -d Ubuntu -u root -e bash -lc "docker ps"                       # ① Docker 引擎在 WSL 内可用
powershell -ExecutionPolicy Bypass -File scripts\infra_up.ps1 -Status  # ② PG 5433 / Redis 6380 连通
.\.venv\Scripts\python.exe -c "from app.config import settings; print(settings.pg_dsn, settings.redis_url)"
curl.exe -s http://127.0.0.1:8000/ready                              # ③ 三依赖探针全 true
wsl -d Ubuntu -u root -e bash -lc "curl -s -o /dev/null -w '%{http_code}' https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"  # ④ SEC 可达
```

