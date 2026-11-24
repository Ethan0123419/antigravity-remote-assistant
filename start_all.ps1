$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$bridgeDir = $PSScriptRoot
Set-Location $bridgeDir

# Ensure agy CLI directory is on PATH if installed in default user locations
foreach ($cand in @(
    (Join-Path $env:LOCALAPPDATA "agy\bin"),
    (Join-Path $env:LOCALAPPDATA "Programs\Antigravity\bin")
)) {
    if ($cand -and (Test-Path $cand)) {
        $env:PATH = "$cand;$env:PATH"
    }
}

# Auto-initialize .env if missing
if (-not (Test-Path (Join-Path $bridgeDir ".env"))) {
    $venvPyInit = Join-Path $bridgeDir ".venv\Scripts\python.exe"
    if (Test-Path $venvPyInit) {
        & $venvPyInit -X utf8 -m qq_agy_bridge.setup_wizard --non-interactive
    }
}

# Read .env for optional NapCat startup
$envMap = @{}
if (Test-Path ".env") {
    Get-Content ".env" -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $parts = $line.Split("=", 2)
            $envMap[$parts[0].Trim()] = $parts[1].Trim()
        }
    }
}

$allowedQQ = $envMap["ALLOWED_QQ_USERS"]
$onebotMode = ($envMap["ONEBOT_ENABLED"] ?? "auto").ToLower()
$enableQQ = ($onebotMode -eq "true" -or $onebotMode -eq "1") -or ($onebotMode -eq "auto" -and -not [string]::IsNullOrWhiteSpace($allowedQQ))

if ($enableQQ) {
    $napcatDir = if ($envMap["NAPCAT_DIR"]) { $envMap["NAPCAT_DIR"] } else { Join-Path (Split-Path $bridgeDir -Parent) "NapCatNode" }
    $botQQ = $envMap["BOT_QQ"]
    $portOpen = (Test-NetConnection -ComputerName 127.0.0.1 -Port 3001 -WarningAction SilentlyContinue).TcpTestSucceeded
    if (-not $portOpen -and (Test-Path (Join-Path $napcatDir "node.exe"))) {
        Write-Host "正在启动 NapCatQQ..." -ForegroundColor Cyan
        $ncArgs = @(".\index.js")
        if ($botQQ) { $ncArgs += @("-q", $botQQ) }
        Start-Process -FilePath (Join-Path $napcatDir "node.exe") `
            -ArgumentList $ncArgs `
            -WorkingDirectory $napcatDir `
            -WindowStyle Minimized
        Start-Sleep -Seconds 5
    }
}

Write-Host "正在启动 Antigravity 私人远程助理..." -ForegroundColor Cyan
$venvPy = Join-Path $bridgeDir ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    & $venvPy -X utf8 -u -m qq_agy_bridge.main
} else {
    uv run --python 3.12 qq-agy-bridge
}
