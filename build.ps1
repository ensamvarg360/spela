# Builds "dist\Spela-<version>.zip" (a Spela folder with Spela.exe), no Python or Node needed to run it.
# A folder, not one .exe: Defender scans a 48 MB one-file exe for ~6 s before it starts, and it unpacks on every launch.
# -Bump patch|minor|major first raises the version in backend\app\__init__.py and rebuilds the helper payload (needs Docker).
# Betas: -Bump patch -Beta makes 0.7.6 into 0.7.7-beta.1, -Bump beta the next beta (0.7.7-beta.2), and -Bump stable
# releases it (0.7.7). Every build people run needs its own version: consoles only get a new helper when it changes.
param([ValidateSet("patch", "minor", "major", "beta", "stable")][string]$Bump, [switch]$Beta)
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true  # stop when npm/pip/pyinstaller fail too
$init = "$PSScriptRoot\backend\app\__init__.py"
if ($Beta -and $Bump -notin "patch", "minor", "major") { throw "-Beta starts the first beta of a new version: add -Bump patch, minor or major." }
if ($Bump) {
    $m = [regex]::Match((Get-Content $init -Raw), '__version__ = "(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?"')
    $major, $minor, $patch = [int[]]$m.Groups[1..3].Value
    $betaOf = if ($m.Groups[4].Success) { [int]$m.Groups[4].Value } else { 0 }
    if ($betaOf -and $Bump -in "patch", "minor", "major") { throw "$major.$minor.$patch-beta.$betaOf is a beta: -Bump beta makes the next one, -Bump stable releases it." }
    if (-not $betaOf -and $Bump -in "beta", "stable") { throw "$major.$minor.$patch isn't a beta. Start one with -Bump patch -Beta." }
    $next = switch ($Bump) {
        "major" { "$($major + 1).0.0" } "minor" { "$major.$($minor + 1).0" } "patch" { "$major.$minor.$($patch + 1)" }
        "beta" { "$major.$minor.$patch-beta.$($betaOf + 1)" } "stable" { "$major.$minor.$patch" }
    }
    if ($Beta) { $next += "-beta.1" }
    (Get-Content $init -Raw) -replace '__version__ = "[^"]+"', "__version__ = `"$next`"" | Set-Content $init -NoNewline
    & "$PSScriptRoot\payload\build.ps1"  # the helper's version must equal the app's
}
$version = [regex]::Match((Get-Content $init -Raw), '__version__ = "([^"]+)"').Groups[1].Value
Push-Location "$PSScriptRoot\frontend"
npm ci --no-fund --no-audit; npm run build
Pop-Location
$venv = "$PSScriptRoot\build\venv"
if (-not (Test-Path $venv)) { python -m venv $venv }
# setuptools reuses backend\build\lib and skips files whose source looks older, which shipped stale code
Remove-Item -Recurse -Force "$PSScriptRoot\backend\build" -ErrorAction SilentlyContinue
& "$venv\Scripts\python" -m pip install --quiet "$PSScriptRoot\backend[desktop]" pyinstaller
# the splash gets the version stamped on its bottom edge, so it always matches the build
& "$venv\Scripts\python" -c @"
from PIL import Image, ImageDraw, ImageFont
img = Image.open(r'$PSScriptRoot\images\splash.png').convert('RGB')
draw = ImageDraw.Draw(img)
draw.text((img.width / 2, img.height - 22), 'v$version', fill=(200, 196, 230), anchor='mm', font=ImageFont.truetype('segoeui.ttf', 16))
img.save(r'$PSScriptRoot\build\splash.png')
"@
Push-Location "$PSScriptRoot\backend"
# --splash shows before any Python runs; launcher.py closes it
$out = "$PSScriptRoot\dist\Spela-$version"
& "$venv\Scripts\python" -m PyInstaller --noconfirm --onedir --windowed --name "Spela" --icon "$PSScriptRoot\backend\app\assets\spela.ico" `
    --splash "$PSScriptRoot\build\splash.png" `
    --distpath $out --workpath "$PSScriptRoot\build" --specpath "$PSScriptRoot\build" `
    --add-data "$PSScriptRoot\frontend\dist;app/static" --add-data "$PSScriptRoot\backend\app\payload;app/payload" `
    --add-data "$PSScriptRoot\backend\app\console_app;app/console_app" `
    --add-data "$PSScriptRoot\backend\app\assets;app/assets" --collect-submodules uvicorn --collect-submodules pystray launcher.py
Pop-Location
Compress-Archive -Force "$out\Spela" "$out.zip"  # unzips to a Spela folder
