# 一条命令看清服务状态：在不在跑、什么时候启动的、端口与健康、V2 模式。
#
# 为什么需要它：start_all.ps1 是启动器，它把监管脚本挂到后台就退出了，
# 所以 IDE / 终端上的「停止」按钮对判断服务状态毫无参考价值——
# 按钮没了不代表没启动，按钮在也不代表启动成功。
#
# 用法：
#   .\scripts\status_all.ps1
#
# 退出码：0 = 服务齐全且健康；1 = 有缺失或不健康。

[CmdletBinding()]
param(
    [int]$ApiPort = 8000
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'process_control.ps1')

function Write-Title([string]$Text) { Write-Host ''; Write-Host $Text -ForegroundColor Cyan }
function Write-Item([string]$Label, [string]$Value, [string]$Color = 'Gray') {
    Write-Host ('  {0,-26}' -f $Label) -NoNewline
    Write-Host $Value -ForegroundColor $Color
}

$healthy = $true

Write-Host ''
Write-Host '企业微信知识机器人 · 运行状态' -ForegroundColor White

# ---------- 进程 ----------
Write-Title '进程'
$supervisors = @(Get-ProjectSupervisorProcesses -ProjectRoot $projectRoot -IncludeRedis)
$services = @(Get-ProjectServiceProcesses)

if ($supervisors.Count -eq 0) {
    Write-Item '监管脚本' '未运行（服务不会自动重启）' 'Yellow'
} else {
    Write-Item '监管脚本' ("{0} 个" -f $supervisors.Count) 'Green'
    foreach ($item in $supervisors) {
        $script = ''
        if ($item.CommandLine -match 'run_(\w+)\.ps1') { $script = $Matches[1] }
        Write-Item ("  {0}" -f $script) ("PID {0}  启动于 {1}" -f $item.ProcessId, $item.CreationDate)
    }
}

if ($services.Count -eq 0) {
    Write-Item '服务进程' '未运行' 'Red'
    $healthy = $false
} else {
    Write-Item '服务进程' ("{0} 个（含 .venv 转发壳）" -f $services.Count) 'Green'
    foreach ($item in $services) {
        $label = if ($item.CommandLine -match 'app\.wecom\.long_connection') { '长连接' } else { 'API' }
        Write-Item ("  {0}" -f $label) ("PID {0}  启动于 {1}" -f $item.ProcessId, $item.CreationDate)
    }
}

# 机器人单独报一行：它是"员工能不能收到回复"的唯一判据，值得一眼看到。
$botProcesses = @($services | Where-Object { $_.CommandLine -match 'app\.wecom\.long_connection' })
if ($botProcesses.Count -eq 0) {
    Write-Item '机器人（企业微信长连接）' '未运行 —— 员工发消息不会有任何回复' 'Red'
    $healthy = $false
} else {
    Write-Item '机器人（企业微信长连接）' '在运行' 'Green'
}

# ---------- 端口 ----------
Write-Title '端口'
$listener = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    Write-Item ("{0} 监听" -f $ApiPort) ("PID {0}" -f $listener.OwningProcess) 'Green'
} else {
    Write-Item ("{0} 监听" -f $ApiPort) '没有在监听' 'Red'
    $healthy = $false
}

# ---------- 健康检查 ----------
Write-Title '健康检查'
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 3
    Write-Item '/health' ("service={0}  status={1}" -f $health.service, $health.status) 'Green'
} catch {
    Write-Item '/health' ("无响应：{0}" -f $_.Exception.Message) 'Red'
    $healthy = $false
}
try {
    $ready = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/ready" -TimeoutSec 8
    $wecom = $ready.dependencies.wecom
    $color = if ($wecom -eq 'online') { 'Green' } else { 'Yellow' }
    Write-Item '/health/ready' ("wecom={0}" -f $wecom) $color
    if ($wecom -ne 'online') { $healthy = $false }
} catch {
    # 机器人不在时这里会超时或报错；把原因写出来，别只说「无响应」。
    Write-Item '/health/ready' ("未通过：{0}" -f $_.Exception.Message) 'Red'
    $healthy = $false
}

# ---------- 配置 ----------
Write-Title '配置'
Write-Item '产品查询链路' 'QueryPlan 审查 + V2 结构化执行（唯一交付链路）' 'Green'

# ---------- 最近日志 ----------
Write-Title '最近的运行日志（文件名带启动时刻）'
Get-ChildItem -Path (Join-Path $projectRoot 'logs') -Filter '*runtime*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 3 |
    ForEach-Object {
        # 文件名比固定标签宽度长，不能用对齐写法，否则会和时间挤在一起
        Write-Host ('  {0}' -f $_.Name)
        Write-Host ('      最后写入 {0}' -f $_.LastWriteTime.ToString('MM-dd HH:mm:ss')) -ForegroundColor DarkGray
    }

Write-Host ''
if ($healthy) {
    Write-Host '结论：服务齐全且健康。' -ForegroundColor Green
    exit 0
}
Write-Host '结论：有缺失或不健康，见上面标红/标黄的项。' -ForegroundColor Yellow
exit 1
