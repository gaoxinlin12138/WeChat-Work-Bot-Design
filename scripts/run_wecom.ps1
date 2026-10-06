$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
$logDirectory = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

$env:PYTHONUNBUFFERED = '1'
while ($true) {
    $runStamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $stdoutLog = Join-Path $logDirectory "wecom_runtime_$runStamp.out.log"
    $stderrLog = Join-Path $logDirectory "wecom_runtime_$runStamp.err.log"
    try {
        Start-Process `
            -FilePath $python `
            -ArgumentList @('-m', 'app.wecom.long_connection') `
            -WorkingDirectory $projectRoot `
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
