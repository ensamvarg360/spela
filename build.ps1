# Builds "dist\PS5 Local Storefront.exe": one file, no Python or Node needed to run it.
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true  # stop when npm/pip/pyinstaller fail too
Push-Location "$PSScriptRoot\frontend"
npm ci --no-fund --no-audit; npm run build
Pop-Location
$venv = "$PSScriptRoot\build\venv"
if (-not (Test-Path $venv)) { python -m venv $venv }
# setuptools reuses backend\build\lib and skips files whose source looks older, which shipped stale code
Remove-Item -Recurse -Force "$PSScriptRoot\backend\build" -ErrorAction SilentlyContinue
& "$venv\Scripts\python" -m pip install --quiet "$PSScriptRoot\backend" pyinstaller
Push-Location "$PSScriptRoot\backend"
# --splash shows while the one-file .exe unpacks, before any Python runs; launcher.py closes it
& "$venv\Scripts\python" -m PyInstaller --noconfirm --onefile --windowed --name "PS5 Local Storefront" `
    --splash "$PSScriptRoot\backend\splash.png" `
    --distpath "$PSScriptRoot\dist" --workpath "$PSScriptRoot\build" --specpath "$PSScriptRoot\build" `
    --add-data "$PSScriptRoot\frontend\dist;app/static" --add-data "$PSScriptRoot\backend\app\payload;app/payload" `
    --add-data "$PSScriptRoot\backend\app\console_app;app/console_app" --collect-submodules uvicorn --collect-submodules pystray launcher.py
Pop-Location
