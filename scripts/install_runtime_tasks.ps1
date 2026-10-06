$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$taskPrefix = 'WeComKnowledgeBot'
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$powerShellExecutable = Join-Path $PSHOME 'powershell.exe'

$principal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$tasks = @(
    @{ Name = "$taskPrefix-Redis"; Script = 'run_redis.ps1' },
    @{ Name = "$taskPrefix-API"; Script = 'run_api.ps1' },
    @{ Name = "$taskPrefix-WeCom"; Script = 'run_wecom.ps1' }
)

foreach ($task in $tasks) {
    $scriptPath = Join-Path $PSScriptRoot $task.Script
    $arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$scriptPath`""
    $action = New-ScheduledTaskAction -Execute $powerShellExecutable -Argument $arguments `
        -WorkingDirectory $projectRoot
    Register-ScheduledTask `
        -TaskName $task.Name `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description 'WeCom knowledge bot runtime supervisor. Starts at user logon and restarts after failures.' `
        -Force | Out-Null
}

Write-Output 'Runtime supervisor tasks registered.'
