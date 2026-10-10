[CmdletBinding()]
param(
    [switch]$NoBrowser,
    # 默认行为是**重启式启动**：先把已有实例真正停掉，再启动。
    # 这样「启动」就一定是真的启动——加载的是当前磁盘上的代码与 .env，
    # 不会再出现「服务还活着就跳过、你以为重启了其实没重启」的情况。
    # 想保留旧的「已在运行就跳过」行为，加 -KeepRunning。
    [switch]$KeepRunning,
    [switch]$IncludeRedis,
    # 默认**前台常驻**：脚本保持运行，于是 IDE / 终端的「停止」按钮（红方块）能真的把
    # 全部服务停掉——监管脚本与服务进程都是本脚本的子进程，会随它一起结束；
    # 按 Ctrl+C 则走 finally 里的清理。
    # 加 -Detach 回到旧的「挂后台就退出」行为（脚本调用场景用，不适合用按钮停）。
    [switch]$Detach
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$envFile = Join-Path $projectRoot '.env'
$adminUrl = 'http://127.0.0.1:8000/admin'
$healthUrl = 'http://127.0.0.1:8000/health'
$readyUrl = 'http://127.0.0.1:8000/health/ready'
$powerShellExecutable = (Get-Process -Id $PID).Path
if (-not $powerShellExecutable -or -not (Test-Path -LiteralPath $powerShellExecutable)) {
    $powerShellExecutable = (Get-Command powershell.exe -ErrorAction Stop).Source
}
# 进程查找与停止的逻辑与 stop_all.ps1 共用一份，避免两处判断不一致。
. (Join-Path $PSScriptRoot 'process_control.ps1')

function Write-Step([string]$Message) {
    Write-Host "[启动] $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message) {
    Write-Host "[成功] $Message" -ForegroundColor Green
}

function Get-DotEnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $envFile)) {
        return $null
    }
    $line = Get-Content -LiteralPath $envFile -Encoding UTF8 |
        Where-Object { $_ -match "^\s*$([regex]::Escape($Name))\s*=" } |
        Select-Object -Last 1
    if (-not $line) {
        return $null
    }
    $value = ($line -split '=', 2)[1].Trim()
    if ($value.Length -ge 2 -and (
        ($value.StartsWith('"') -and $value.EndsWith('"')) -or
        ($value.StartsWith("'") -and $value.EndsWith("'"))
    )) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

function Test-TcpPort([int]$Port) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        return $task.Wait(800) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-ApiHealth {
    try {
        $response = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($response.service -eq 'wecom-knowledge-bot' -and $response.status -eq 'ok') {
            return $response
        }
    } catch {
        return $null
    }
    return $null
}

function Test-AdminPage {
    try {
        $response = Invoke-WebRequest -Uri $adminUrl -UseBasicParsing -TimeoutSec 3
        return $response.StatusCode -eq 200
    } catch {
        if ($_.Exception.Response) {
            return [int]$_.Exception.Response.StatusCode -eq 401
        }
        return $false
    }
}

function Test-WeComOnline {
    try {
        $response = Invoke-RestMethod -Uri $readyUrl -TimeoutSec 3
        return $response.dependencies.wecom -eq 'online'
    } catch {
        return $false
    }
}

function Test-ProjectProcess([string]$Pattern) {
    $escapedRoot = [regex]::Escape($projectRoot)
    return [bool](Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine -match $Pattern -and
            $_.CommandLine -match $escapedRoot
        } |
        Select-Object -First 1)
}

function Start-Supervisor([string]$ScriptName) {
    $scriptPath = Join-Path $PSScriptRoot $ScriptName
    Start-Process `
        -FilePath $powerShellExecutable `
        -ArgumentList @(
            '-NoProfile',
            '-NonInteractive',
            '-WindowStyle', 'Hidden',
            '-ExecutionPolicy', 'Bypass',
            '-File', "`"$scriptPath`""
        ) `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden | Out-Null
}

function Wait-Until([scriptblock]$Condition, [int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        if (& $Condition) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

try {
    Set-Location -LiteralPath $projectRoot
    Write-Host ''
    Write-Host '企业微信知识库 · 全部服务一键启动' -ForegroundColor White
    Write-Host '--------------------------------' -ForegroundColor DarkGray

    if (-not (Test-Path -LiteralPath $python)) {
        throw '未找到 .venv\Scripts\python.exe，请先安装项目依赖。'
    }
    if (-not (Test-Path -LiteralPath $envFile)) {
        throw '未找到 .env 配置文件，请先根据 .env.example 完成配置。'
    }

    $redisUrl = Get-DotEnvValue 'REDIS_URL'
    if ($redisUrl) {
        if (Test-TcpPort 6379) {
            Write-Ok 'Redis 已在运行'
        } else {
            Write-Step '正在启动 Redis…'
            if (-not (Test-ProjectProcess 'run_redis\.ps1')) {
                Start-Supervisor 'run_redis.ps1'
            }
            if (-not (Wait-Until { Test-TcpPort 6379 } 15)) {
                throw 'Redis 启动失败。请查看 logs\redis_runtime_*.err.log。'
            }
            Write-Ok 'Redis 已启动'
        }
    } else {
        Write-Host '[跳过] .env 未配置 REDIS_URL' -ForegroundColor Yellow
    }

    if (-not $KeepRunning) {
        # 启动即真正启动：先停干净再拉起来。
        # 不停掉旧实例的话，新进程要么绑不上 8000 端口、要么（企业微信长连接）连不上，
        # 最后仍然是旧进程在服务——表面上「重启了」，实际跑的还是老代码。
        Write-Step '正在停止已有实例（重启式启动；加 -KeepRunning 可跳过）…'
        if (-not (Stop-ProjectProcesses -ProjectRoot $projectRoot -IncludeRedis:$IncludeRedis)) {
            throw '已有实例未能全部停止，为避免「旧进程继续服务」而中止启动。请先运行 scripts\stop_all.ps1 看是谁没停掉。'
        }
        if (-not (Wait-PortReleased -Port 8000 -TimeoutSeconds 15)) {
            throw '端口 8000 在停止后仍未释放，启动会绑不上。'
        }
        Write-Ok '已有实例已停止'
    }

    if ((Get-ApiHealth) -and (Test-AdminPage)) {
        Write-Ok '后端 API 与管理台前端已在运行'
    } else {
        if ((Test-TcpPort 8000) -and -not (Test-ProjectProcess 'app\.main:app|run_api\.ps1')) {
            throw '端口 8000 已被其他程序占用，无法启动后端与管理台。'
        }
        Write-Step '正在启动后端 API 与管理台前端…'
        if (-not (Test-ProjectProcess 'app\.main:app|run_api\.ps1')) {
            Start-Supervisor 'run_api.ps1'
        }
        if (-not (Wait-Until { [bool](Get-ApiHealth) -and (Test-AdminPage) } 45)) {
            throw '后端或管理台启动失败。请查看 logs\api_runtime_*.err.log。'
        }
        Write-Ok '后端 API 与管理台前端已启动'
    }

    $botId = Get-DotEnvValue 'WECOM_BOT_ID'
    $botSecret = Get-DotEnvValue 'WECOM_BOT_SECRET'
    if ($botId -and $botSecret) {
        if (Test-ProjectProcess 'app\.wecom\.long_connection|run_wecom\.ps1') {
            Write-Step '企业微信机器人进程已存在，正在确认认证状态…'
        } else {
            Write-Step '正在启动企业微信机器人…'
            Start-Supervisor 'run_wecom.ps1'
            if (-not (Wait-Until { Test-ProjectProcess 'app\.wecom\.long_connection|run_wecom\.ps1' } 10)) {
                throw '企业微信机器人启动失败。请查看 logs\wecom_runtime_*.err.log。'
            }
        }
        if (Wait-Until { Test-WeComOnline } 45) {
            Write-Ok '企业微信机器人已认证并保持长连接'
        } else {
            Write-Host '[提醒] 企业微信机器人进程已启动，但尚未确认长连接认证成功。' -ForegroundColor Yellow
        }
    } else {
        Write-Host '[跳过] 未配置 WECOM_BOT_ID 或 WECOM_BOT_SECRET' -ForegroundColor Yellow
    }

    try {
        $readiness = Invoke-RestMethod -Uri $readyUrl -TimeoutSec 8
        if ($readiness.status -eq 'ready') {
            Write-Ok '数据库、Redis 和模型服务配置检查通过'
        }
    } catch {
        Write-Host '[提醒] 管理台已启动，但依赖检查未全部通过；可在管理台继续查看。' -ForegroundColor Yellow
    }

    if (-not $NoBrowser) {
        Start-Process -FilePath 'explorer.exe' -ArgumentList $adminUrl
    }
    Write-Host ''
    Write-Host "管理台：$adminUrl" -ForegroundColor White

    if ($Detach) {
        Write-Host '全部服务已在后台就绪（-Detach）。本窗口可以关闭，服务会继续运行。' -ForegroundColor White
        Write-Host '注意：这种方式下 IDE 的停止按钮停不掉服务，请用 scripts\stop_all.ps1。' -ForegroundColor Yellow
        exit 0
    }

    Write-Host '全部服务已就绪。' -ForegroundColor White
    Write-Host '本窗口保持运行：按 Ctrl+C，或点 IDE 的红色停止按钮，即可把全部服务停掉。' -ForegroundColor White
    Write-Host '想单独看状态用 scripts\status_all.ps1；想只停不启用 scripts\stop_all.ps1。' -ForegroundColor DarkGray

    # 前台常驻。停下来的两条路：
    #   1. Ctrl+C（或终端的停止）→ PowerShell 抛停止信号，finally 会执行；
    #   2. IDE 的红色停止按钮 → 由下面的父进程看护兜底。
    # 因此这里不放"自动重启监管脚本"的逻辑：监管脚本自己就是 while($true) 重启循环，
    # 再套一层会互相打架。
    try {
        # 盯住父进程（IDE 里是 一键启动项目.py，终端里是那个终端进程）。
        # 为什么要盯：IDE 的停止按钮未必会连进程树一起杀。若它只杀掉 Python 启动器，
        # 本脚本与下面的监管脚本、服务进程就成了孤儿，继续在后台跑——看起来"停了"，其实没有。
        # 父进程一消失就跳出循环，走 finally 把全部服务停干净，这样按钮才真的等于"全部停掉"。
        $parentProcessId = (
            Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction SilentlyContinue
        ).ParentProcessId
        # 只有**确实认到了一个活着的父进程**才开看护。
        # 否则（父进程已经不在、或拿不到进程号）后面每轮都会判定"父进程没了"，
        # 服务刚起来就被自己清掉——那比不留孤儿更糟。
        $watchParent = $false
        $parentStartedAt = $null
        if ($parentProcessId) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentProcessId" -ErrorAction SilentlyContinue
            if ($parent) {
                $parentStartedAt = $parent.CreationDate
                $watchParent = $true
            }
        }
        if (-not $watchParent) {
            Write-Host '[提醒] 未能识别父进程，停止按钮的兜底清场不可用；停止请用 scripts\stop_all.ps1。' -ForegroundColor Yellow
        }

        while ($true) {
            Start-Sleep -Seconds 2
            if (-not $watchParent) { continue }
            $current = Get-CimInstance Win32_Process -Filter "ProcessId=$parentProcessId" -ErrorAction SilentlyContinue
            # 进程号会被复用：连创建时间一起比对，免得"父进程换了个人"却被当成还活着
            if (-not $current -or $current.CreationDate -ne $parentStartedAt) {
                Write-Host ''
                Write-Host '[停止] 启动器已退出（IDE 停止按钮 / 终端关闭），正在停掉全部服务…' -ForegroundColor Cyan
                break
            }
        }
    } finally {
        Write-Host ''
        Write-Host '[停止] 正在停止全部服务…' -ForegroundColor Cyan
        $stopped = Stop-ProjectProcesses -ProjectRoot $projectRoot -IncludeRedis:$IncludeRedis -Quiet
        if ($stopped -and (Wait-PortReleased -Port 8000 -TimeoutSeconds 10)) {
            Write-Host '[成功] 已全部停止' -ForegroundColor Green
        } else {
            Write-Host '[提醒] 仍有残留，运行 scripts\stop_all.ps1 查看是谁。' -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host ''
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host '日志目录：' -NoNewline
    Write-Host (Join-Path $projectRoot 'logs') -ForegroundColor Yellow
    exit 1
}
