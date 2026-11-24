$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path ".env")) {
    if (Test-Path $venvPy) {
        & $venvPy -X utf8 -m qq_agy_bridge.setup_wizard --non-interactive
    } else {
        Copy-Item ".env.example" ".env"
    }
}

if (Test-Path $venvPy) {
    & $venvPy -X utf8 -u -m qq_agy_bridge.main
} else {
    uv run --python 3.12 qq-agy-bridge
}
