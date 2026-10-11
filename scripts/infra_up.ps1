# 基础设施一键启停脚本（本机环境：Docker 引擎运行于 WSL Ubuntu，docker.io 官方源被墙需走镜像）
#
# 覆盖服务：PostgreSQL（任务状态/中间产出/报告）+ Redis（Celery broker/结果后端/缓存/限流）
#   注：本项目为文本多 Agent 工作流，无向量检索，Milvus 及其依赖 etcd、minio 已从编排中移除，
#       因此不再做 Milvus 健康检查；改为等待 postgres + redis 双双 healthy。
#   编排文件是 docker-compose.infra.yml（**不是**项目一的 docker-compose.yml）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\infra_up.ps1          # 启动（含保活）
#   powershell -ExecutionPolicy Bypass -File scripts\infra_up.ps1 -Down    # 停止
#   powershell -ExecutionPolicy Bypass -File scripts\infra_up.ps1 -Status  # 查看状态
#   powershell -ExecutionPolicy Bypass -File scripts\infra_up.ps1 -Keepalive  # 启动并前台保活（窗口需保持打开）
#
# 路径说明：项目目录由脚本自身位置推导（$PSScriptRoot 的上级目录）并自动转换为 WSL 路径，
#           因此脚本无论如何被调用都不会指到别的项目（如项目一的 RAG-Agent）。
# 注意：本文件必须保存为 UTF-8 with BOM，否则 Windows PowerShell 5.1 会按 GBK 解码中文导致语法错误。
param(
    [switch]$Down,
    [switch]$Status,
    [switch]$Keepalive
)

$compose = "docker-compose.infra.yml"        # 基础设施编排（PostgreSQL + Redis）
$infraServices = "postgres redis"
$containers = "research-postgres research-redis"
$ubuntu = "Ubuntu"

$projectRoot = Split-Path -Parent $PSScriptRoot          # Windows 侧项目根目录

function ConvertTo-WslPath {
    param([string]$WindowsPath)
    $p = $WindowsPath -replace '\\', '/'
    if ($p -match '^([A-Za-z]):/(.*)$') { return "/mnt/$($matches[1].ToLower())/$($matches[2])" }
    throw "无法把 Windows 路径转换为 WSL 路径：$WindowsPath"
}

$projectDir = ConvertTo-WslPath $projectRoot             # 同一目录在 WSL 内的路径（/mnt/d/...）

function Invoke-Wsl {
    param([string]$Cmd)
    wsl -d $ubuntu -u root -e bash -lc $Cmd 2>&1
}

$composePath = Join-Path $projectRoot $compose
if (-not (Test-Path $composePath)) {
    Write-Warning "[infra] 找不到编排文件：$composePath"
    Write-Warning "[infra] 本项目基础设施编排为 docker-compose.infra.yml（仅 PostgreSQL + Redis），请确认文件存在。"
    exit 1
}

# docker compose 只默认读取 .env，识别不了 .env.local；而宿主端口（PG_PORT/REDIS_PORT）只在
# env 文件里定义（编排里故意不给默认值），因此按优先级显式传入两个 env-file（后者覆盖前者）。
$envFileArgs = @()
foreach ($ef in '.env', '.env.local') {
    if (Test-Path (Join-Path $projectRoot $ef)) { $envFileArgs += "--env-file $ef" }
}
$envFileArgs = ($envFileArgs -join ' ')

# 读取实际生效的宿主端口（.env → .env.local 覆盖），用于状态探测与提示信息
function Get-EnvValue {
    param([string]$Key, [string]$Default)
    $value = $Default
    foreach ($ef in '.env', '.env.local') {
        $path = Join-Path $projectRoot $ef
        if (-not (Test-Path $path)) { continue }
        $hit = Select-String -Path $path -Pattern "^$Key=(\S+)" | Select-Object -Last 1
        if ($hit) { $value = $hit.Matches[0].Groups[1].Value }
    }
    return $value
}

$pgPort = Get-EnvValue 'PG_PORT' '5432'
$redisPort = Get-EnvValue 'REDIS_PORT' '6379'

# ---------------------------------------------------------------------------
# 保活：WSL VM 在没有活跃客户端连接时会被宿主回收（即使配置了 vmIdleTimeout=0），
# 表现为容器随之停止、应用侧报 PostgreSQL/Redis 连接拒绝。
#
# 实测结论：用 Start-Process 启动的"分离进程"会被宿主进程树回收，起不到保活作用；
# 只有前台常驻的会话才能真正把 VM 挂住。因此提供 -Keepalive：前台守护，请保持窗口打开。
# ---------------------------------------------------------------------------
function Start-KeepaliveForeground {
    Write-Host "[infra] keepalive mode: 正在前台保持 WSL 会话（Ctrl+C 退出）..."
    while ($true) {
        wsl -d $ubuntu -u root -e bash -lc "while true; do sleep 20; done"
        Write-Host "[infra] WSL 会话中断，5 秒后自动重连并恢复容器..."
        Start-Sleep -Seconds 5
        Invoke-Wsl "systemctl start docker; cd '$projectDir' && docker compose $envFileArgs -f $compose up -d $infraServices 2>&1 | tail -3"
    }
}

if ($Down) {
    Invoke-Wsl "cd '$projectDir' && docker compose $envFileArgs -f $compose down 2>&1 | tail -3"
    Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and (
                $_.CommandLine -match "keepalive-hold" -or
                $_.CommandLine -match "while true; do sleep 20"
            )
        } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Write-Host "[infra] stopped (containers + keepalive)"
    exit 0
}

if ($Status) {
    Write-Host "== WSL =="
    Invoke-Wsl "uptime"
    Write-Host "== containers =="
    Invoke-Wsl "docker ps -a --format '{{.Names}} {{.Status}}'"
    Write-Host "== windows-side ports =="
    foreach ($p in [int]$pgPort, [int]$redisPort) {
        $state = "DOWN"
        try {
            $c = New-Object System.Net.Sockets.TcpClient
            $iar = $c.BeginConnect("127.0.0.1", $p, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(2500) -and $c.Connected) { $state = "CONNECTED" }
            $c.Close()
        } catch { }
        Write-Host ("  127.0.0.1:{0} -> {1}" -f $p, $state)
    }
    exit 0
}

Write-Host "[infra] project: $projectRoot  ->  WSL: $projectDir"
Write-Host "[infra] ensuring docker daemon in WSL..."
Invoke-Wsl "systemctl enable docker >/dev/null 2>&1; systemctl start docker; sleep 3"
Write-Host "[infra] starting containers (registry mirror: dockerproxy.net)..."
Invoke-Wsl "cd '$projectDir' && docker compose $envFileArgs -f $compose up -d $infraServices 2>&1 | tail -6"
Invoke-Wsl "docker update --restart unless-stopped $containers >/dev/null 2>&1"

Write-Host "[infra] waiting for healthy (postgres + redis)..."
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    $pgStatus = Invoke-Wsl "docker inspect -f '{{.State.Health.Status}}' research-postgres 2>/dev/null"
    $redisStatus = Invoke-Wsl "docker inspect -f '{{.State.Health.Status}}' research-redis 2>/dev/null"
    if (($pgStatus -match "healthy") -and ($redisStatus -match "healthy")) {
        Write-Host "[infra] postgres + redis healthy after $($i*2+2)s"
        $healthy = $true
        break
    }
}
if (-not $healthy) {
    Write-Warning "[infra] 等待健康检查超时，请查看：docker ps -a / docker logs research-postgres / docker logs research-redis"
}

Invoke-Wsl "docker ps --format '{{.Names}} {{.Status}}'"

if ($Keepalive) {
    Start-KeepaliveForeground
} else {
    Write-Host "[infra] done. 应用侧连接：PostgreSQL 127.0.0.1:$pgPort（research/researchpass/research）、Redis 127.0.0.1:$redisPort"
    Write-Host "[infra] Redis 分库：db0 限流+缓存 / db1 Celery broker / db2 Celery 结果"
    Write-Host "[infra] 提示：WSL VM 在无活跃会话时会被宿主回收，建议使用 -Keepalive 参数常驻保活。"
}
