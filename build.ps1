$ErrorActionPreference = "Stop"
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "========================================"
Write-Host " Money App - PyInstaller Build"
Write-Host "========================================"

Set-Location $ROOT

Write-Host "[1/3] Checking PyInstaller..."
& "$ROOT\venv\Scripts\pip.exe" install pyinstaller --quiet

Write-Host "[2/3] Cleaning previous build..."
if (Test-Path "$ROOT\dist\MoneyApp") { Remove-Item "$ROOT\dist\MoneyApp" -Recurse -Force }
if (Test-Path "$ROOT\build")         { Remove-Item "$ROOT\build"         -Recurse -Force }

Write-Host "[3/3] Building... (5-10 min)"
& "$ROOT\venv\Scripts\pyinstaller.exe" `
  --onedir `
  --name MoneyApp `
  --add-data "$ROOT\templates;templates" `
  --add-data "$ROOT\static;static" `
  --collect-all yfinance `
  --collect-all curl_cffi `
  --collect-all certifi `
  --collect-all pandas `
  --collect-all sqlalchemy `
  --collect-all jinja2 `
  --collect-all starlette `
  --hidden-import sqlalchemy.dialects.sqlite `
  --hidden-import uvicorn.logging `
  --hidden-import uvicorn.loops `
  --hidden-import uvicorn.loops.auto `
  --hidden-import uvicorn.protocols `
  --hidden-import uvicorn.protocols.http `
  --hidden-import uvicorn.protocols.http.auto `
  --hidden-import uvicorn.lifespan `
  --hidden-import uvicorn.lifespan.on `
  --distpath "$ROOT\dist" `
  --workpath "$ROOT\build" `
  --specpath "$ROOT" `
  "$ROOT\app_main.py"

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "========================================"
    Write-Host " Build complete!"
    Write-Host " Location: $ROOT\dist\MoneyApp\MoneyApp.exe"
    Write-Host "========================================"
} else {
    Write-Host " Build failed. Check errors above."
}

Read-Host "Press Enter to close"
