$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $projectRoot 'logs'
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'WeComKnowledgeBot\redis'
$redisData = Join-Path $runtimeRoot 'data\redis'
New-Item -ItemType Directory -Force -Path $redisData, $logDirectory | Out-Null

$redisServer = Get-Command redis-server.exe -ErrorAction SilentlyContinue
if (-not $redisServer) {
    $packageRoot = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    $redisServer = Get-ChildItem -LiteralPath $packageRoot -Recurse -File `
        -Filter 'redis-server.exe' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}
if (-not $redisServer) {
    throw 'redis-server.exe was not found. Reinstall the Redis Windows portable package.'
}

$executable = if ($redisServer.Source) { $redisServer.Source } else { $redisServer.FullName }
$configurationTemplate = Join-Path $projectRoot 'config\redis.conf'
$configuration = Join-Path $runtimeRoot 'redis.conf'
Copy-Item -LiteralPath $configurationTemplate -Destination $configuration -Force
Set-Location -LiteralPath $runtimeRoot
while ($true) {
    $runStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdoutLog = Join-Path $logDirectory "redis_runtime_$runStamp.out.log"
    $stderrLog = Join-Path $logDirectory "redis_runtime_$runStamp.err.log"
    try {
        Start-Process `
            -FilePath $executable `
            -ArgumentList @('redis.conf') `
            -WorkingDirectory $runtimeRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdoutLog `
            -RedirectStandardError $stderrLog `
            -Wait | Out-Null
    } catch {
        Start-Sleep -Seconds 5
        continue
    }
    Start-Sleep -Seconds 5
}
