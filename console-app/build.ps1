# Builds the PS5 app into backend\app\console_app\PPSA99787 in a Linux container. Needs Docker Desktop.
# The built folder is committed, so ..\build.ps1 doesn't need Docker. Run this after changing console-app\.
#   ./build.ps1 -Preview   renders the screen on this PC instead (console-app\preview\*.png), with a fake PC app
param([switch]$Preview)
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$mode = if ($Preview) { "preview" } else { "app" }
docker volume create ps5-console-app-deps | Out-Null
docker run --rm -v "$PSScriptRoot\..:/repo" -v ps5-console-app-deps:/deps ubuntu:24.04 bash /repo/console-app/build.sh $mode
