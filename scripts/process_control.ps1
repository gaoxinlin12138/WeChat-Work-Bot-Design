# 项目进程的查找与停止。start_all.ps1 与 stop_all.ps1 共用这一份，避免两处判断不一致。
#
# 两个必须知道的坑（历史日志里的 10048「端口已被占用」就是踩了它们）：
#
# 1. `.venv\Scripts\python.exe` 只是转发壳，真正占端口的是它派生出来的基础解释器进程。
#    壳的命令行里有项目路径，基础解释器进程的命令行里**没有**（只有 `-m uvicorn app.main:app`）。
#    所以服务进程必须按**项目特有的模块路径**匹配；按项目路径过滤会漏杀基础解释器，
#    留下一个占着 8000 端口、又没有任何监管的孤儿进程。
#
# 2. `run_api.ps1` / `run_wecom.ps1` 是 `while ($true)` 循环：子进程退出后 5 秒会重新拉起。
#    所以**必须先杀监管脚本、再杀服务本体**，顺序反了等于白杀。

Set-StrictMode -Version Latest

function Get-ProjectServiceProcesses {
    <#
      .SYNOPSIS
      找出本项目的服务进程（API 与企业微信长连接）。
      .DESCRIPTION
      按模块路径匹配，不按项目路径匹配——原因见文件头第 1 条。
    #>
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match 'app\.main:app|app\.wecom\.long_connection'
    }
}

function Get-ProjectSupervisorProcesses {
    <#
      .SYNOPSIS
      找出本项目的监管脚本进程（run_api.ps1 / run_wecom.ps1，可选 run_redis.ps1）。
    #>
    param(
        [Parameter(Mandatory)][string]$ProjectRoot,
        [switch]$IncludeRedis
    )
    $pattern = 'run_api\.ps1|run_wecom\.ps1'
    if ($IncludeRedis) {
        $pattern = "$pattern|run_redis\.ps1"
    }
    $escapedRoot = [Regex]::Escape($ProjectRoot)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match $pattern -and
        $_.CommandLine -match $escapedRoot
    }
}

function Wait-PortReleased {
    <#  等端口真正释放。进程刚被杀掉时端口可能还没回收。 #>
    param(
        [Parameter(Mandatory)][int]$Port,
        [int]$TimeoutSeconds = 15
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if (-not $listener) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-ProjectProcesses {
    <#
      .SYNOPSIS
      真正停掉本项目的进程；返回是否全部停干净。
      .DESCRIPTION
      循环「杀一轮 → 确认 → 再杀一轮」，直到没有残留或超时。
      监管脚本用 taskkill /T 连子进程一起杀，随后再按模块路径兜底扫一遍。
    #>
    param(
        [Parameter(Mandatory)][string]$ProjectRoot,
        [switch]$IncludeRedis,
        [int]$TimeoutSeconds = 25,
        [switch]$Quiet
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $reported = New-Object System.Collections.Generic.HashSet[int]

    while ($true) {
        # 监管在前、服务在后：先断掉自动重启
        $targets = @(
            Get-ProjectSupervisorProcesses -ProjectRoot $ProjectRoot -IncludeRedis:$IncludeRedis
            Get-ProjectServiceProcesses
        ) | Sort-Object ProcessId -Unique

        if (-not $targets) {
            break
        }
        foreach ($target in $targets) {
            if ($reported.Add([int]$target.ProcessId) -and -not $Quiet) {
                Write-Host ("  停止 PID {0}  {1}" -f $target.ProcessId, $target.Name) -ForegroundColor DarkGray
            }
            # /T 连同子进程一起杀
            & taskkill.exe /PID $target.ProcessId /T /F 2>&1 | Out-Null
        }
        if ((Get-Date) -ge $deadline) {
            break
        }
        Start-Sleep -Milliseconds 800
    }

    if ($IncludeRedis) {
        Get-Process -Name 'redis-server' -ErrorAction SilentlyContinue | ForEach-Object {
            if (-not $Quiet) {
                Write-Host ("  停止 Redis PID {0}" -f $_.Id) -ForegroundColor DarkGray
            }
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }
    }

    $leftServices = @(Get-ProjectServiceProcesses).Count
    $leftSupervisors = @(
        Get-ProjectSupervisorProcesses -ProjectRoot $ProjectRoot -IncludeRedis:$IncludeRedis
    ).Count
    return ($leftServices -eq 0 -and $leftSupervisors -eq 0)
}
