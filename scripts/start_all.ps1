[CmdletBinding()]
param(
    [switch]$NoBrowser
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
    Write-Host '全部服务已就绪。这个窗口可以关闭，后台服务会继续运行。' -ForegroundColor White
    exit 0
} catch {
    Write-Host ''
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host '日志目录：' -NoNewline
    Write-Host (Join-Path $projectRoot 'logs') -ForegroundColor Yellow
    exit 1
}
