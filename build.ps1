# Builds "dist\PS5 Local Storefront.exe": one file, no Python or Node needed to run it.
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true  # stop when npm/pip/pyinstaller fail too
Push-Location "$PSScriptRoot\frontend"
npm ci --no-fund --no-audit; npm run build
Pop-Location
$venv = "$PSScriptRoot\build\venv"
if (-not (Test-Path $venv)) { python -m venv $venv }
& "$venv\Scripts\python" -m pip install --quiet "$PSScriptRoot\backend" pyinstaller
Push-Location "$PSScriptRoot\backend"
& "$venv\Scripts\python" -m PyInstaller --noconfirm --onefile --windowed --name "PS5 Local Storefront" `
    --distpath "$PSScriptRoot\dist" --workpath "$PSScriptRoot\build" --specpath "$PSScriptRoot\build" `
    --add-data "$PSScriptRoot\frontend\dist;app/static" --add-data "$PSScriptRoot\backend\app\payload;app/payload" `
    --add-data "$PSScriptRoot\backend\app\console_app;app/console_app" --collect-submodules uvicorn --collect-submodules pystray launcher.py
Pop-Location
