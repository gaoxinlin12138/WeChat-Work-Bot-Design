# 真正停掉本项目的所有进程：后端 API、企业微信长连接，以及拉起它们的监管脚本。
#
# 用法：
#   .\scripts\stop_all.ps1                  # 停 API 与企业微信机器人
#   .\scripts\stop_all.ps1 -IncludeRedis    # 连 Redis 一起停
#
# 退出码：0 = 确认全部停止；1 = 仍有残留（脚本会打印是谁还占着端口）。

[CmdletBinding()]
param(
    [switch]$IncludeRedis,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'process_control.ps1')

function Write-Step([string]$Message) { Write-Host "[停止] $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[成功] $Message" -ForegroundColor Green }
function Write-Fail([string]$Message) { Write-Host "[失败] $Message" -ForegroundColor Red }

Write-Step '正在停止后端 API 与企业微信机器人…'
if (-not $Quiet) {
    Write-Host '  （先停监管脚本再停服务：监管脚本会在子进程退出后 5 秒重新拉起它）' -ForegroundColor DarkGray
}

$clean = Stop-ProjectProcesses -ProjectRoot $projectRoot -IncludeRedis:$IncludeRedis -Quiet:$Quiet

$portFree = Wait-PortReleased -Port 8000 -TimeoutSeconds 15

if ($clean -and $portFree) {
    $what = if ($IncludeRedis) { 'API、企业微信长连接、Redis' } else { 'API、企业微信长连接' }
    Write-Ok "已全部停止（$what）"
    exit 0
}

Write-Fail '仍有进程未停止：'
@(
    Get-ProjectSupervisorProcesses -ProjectRoot $projectRoot -IncludeRedis:$IncludeRedis
    Get-ProjectServiceProcesses
) | Sort-Object ProcessId -Unique | ForEach-Object {
    Write-Host ("  PID {0}  {1}" -f $_.ProcessId, $_.CommandLine) -ForegroundColor Red
}
if (-not $portFree) {
    Write-Host '端口 8000 仍被占用，占用者：' -ForegroundColor Red
    Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host ("  PID {0}" -f $_.OwningProcess) -ForegroundColor Red
    }
}
exit 1
