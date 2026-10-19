#Requires -Version 7.3
# Builds a release into dist\release\v<version>\: the .exe (it bundles the helper and the PS5 app), SHA256SUMS.txt
# and RELEASE_NOTES.md (collected from the `Changelog:` trailers of the commits since the last tag).
#   ./release.ps1                          build the current version; nothing is committed, tagged or pushed
#   ./release.ps1 -Bump patch              raise the version first (patch|minor|major) and rebuild the helper (needs Docker);
#                                          the bump stays uncommitted in your working tree until -Publish
#   ./release.ps1 -Bump patch -Publish     also commit the bump, tag v<version>, push, and create the GitHub release (needs gh)
#   ./release.ps1 -Publish -Draft          create the release as a draft, to look it over on GitHub before it goes live
# The helper and PS5 app committed under backend\app are bundled in the .exe (rebuild them with payload\build.ps1 and console-app\build.ps1).
# They aren't separate downloads: both only work once Spela has paired them with a console, which only the app can do.
param(
    [ValidateSet("patch", "minor", "major")][string]$Bump,
    [switch]$Publish,
    [switch]$Draft,
    [switch]$SkipTests
)
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$root = $PSScriptRoot
$init = "$root\backend\app\__init__.py"
$helperSource = "$root\backend\app\payload\spela.elf"
Set-Location $root

function Get-Version { [regex]::Match((Get-Content $init -Raw), '__version__ = "([^"]+)"').Groups[1].Value }

if ($Publish) {
    gh auth status *> $null
    # a bump from an earlier ./release.ps1 -Bump run (the version and the rebuilt helper) is fine: it gets committed below
    if (git status --porcelain -- . ":!backend/app/__init__.py" ":!backend/app/payload/spela.elf") {
        throw "The working tree has uncommitted changes. Commit or stash them first."
    }
    git fetch --quiet origin
    if ((git rev-list --count "HEAD..@{u}") -ne "0") { throw "This branch is behind origin. Pull first." }
}
if ($Draft -and -not $Publish) { throw "-Draft only makes sense with -Publish." }

# The version bump and the rebuild of the helper (a test fails if its version differs from the app's).
$buildArgs = @{}
if ($Bump) { $buildArgs.Bump = $Bump }
& "$root\build.ps1" @buildArgs
$version = Get-Version
$tag = "v$version"
if ($Publish -and (git tag --list $tag)) { throw "Tag $tag already exists. Use -Bump to make a new version." }

if (-not $SkipTests) {
    $venv = "$root\build\venv"
    & "$venv\Scripts\python" -m pip install --quiet "$root\backend[test]"
    Push-Location "$root\backend"
    try { & "$venv\Scripts\python" -m pytest -q } finally { Pop-Location }
}

# Release notes: the `Changelog: <Section>: <text>` trailers since the previous tag, grouped by section.
$previous = git tag --list "v*" --sort=-v:refname | Where-Object { $_ -ne $tag } | Select-Object -First 1
$range = if ($previous) { "$previous..HEAD" } else { "HEAD" }
$sections = [ordered]@{ Added = @(); Changed = @(); Fixed = @(); Removed = @(); Security = @() }
$missing = @()
foreach ($commit in (git log $range --format="%B%x1e") -join "`n" -split "\x1e") {
    $lines = $commit.Trim() -split "\r?\n"
    if (-not $lines[0]) { continue }
    $trailers = $lines | Where-Object { $_ -match '^Changelog:\s*' }
    if (-not $trailers) { $missing += $lines[0]; continue }
    foreach ($t in $trailers) {
        if ($t -match '^Changelog:\s*(Added|Changed|Fixed|Removed|Security):\s*(.+)$') { $sections[$Matches[1]] += $Matches[2].Trim() }
        elseif ($t -notmatch '^Changelog:\s*skip\s*$') { Write-Warning "Unreadable trailer in '$($lines[0])': $t" }
    }
}
if ($missing) { Write-Warning ("Commits without a Changelog: trailer (left out of the notes):`n  " + ($missing -join "`n  ")) }

$out = "$root\dist\release\$tag"
Remove-Item -Recurse -Force $out -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $out | Out-Null
$exe = "Spela-$version.exe"
Copy-Item "$root\dist\$exe" "$out\$exe"
$assets = @($exe)
$sums = foreach ($a in $assets) { "{0} *{1}" -f (Get-FileHash "$out\$a" -Algorithm SHA256).Hash.ToLower(), $a }
($sums -join "`n") + "`n" | Set-Content "$out\SHA256SUMS.txt" -NoNewline

$notes = [Collections.Generic.List[string]]::new()
foreach ($s in $sections.Keys) {
    if ($sections[$s].Count) { $notes.Add("### $s"); $notes.AddRange([string[]]($sections[$s] | ForEach-Object { "- $_" })); $notes.Add("") }
}
if (-not ($sections.Values | Where-Object { $_.Count })) { $notes.Add("Maintenance release."); $notes.Add("") }
$notes.Add("### Downloads")
$notes.Add("")
$notes.Add("| File | What it is |")
$notes.Add("|---|---|")
$notes.Add("| ``$exe`` | **The app for Windows.** This is what most people want: download it and double-click. |")
$notes.Add("| ``SHA256SUMS.txt`` | Checksums. In PowerShell: ``Get-FileHash $exe`` and compare. |")
$notes.Add("")
$notes.Add("**Spela provides no game files.** It only works with backups you made yourself of games you own.")
$notes.Add("")
$notes.Add("Windows SmartScreen may warn about an unsigned app the first time. See the [README](https://github.com/ensamvarg360/spela#readme) for setup and what it needs.")
($notes -join "`n") | Set-Content "$out\RELEASE_NOTES.md"

Write-Host "`nRelease $tag built in $out`n" -ForegroundColor Green
Get-ChildItem $out | Select-Object Name, @{ n = "MB"; e = { [math]::Round($_.Length / 1MB, 2) } } | Format-Table -AutoSize

if (-not $Publish) {
    Write-Host "Not published. Read $out\RELEASE_NOTES.md, then re-run with -Publish (add -Bump <part> for a new version)."
    return
}
git add $init $helperSource
if (git diff --cached --name-only) { git commit -m "chore: release $tag" -m "Changelog: skip" }
git tag -a $tag -m "Spela $tag"
git push origin HEAD
git push origin $tag
$flags = @(if ($Draft) { "--draft" })
gh release create $tag @flags --title "Spela $tag" --notes-file "$out\RELEASE_NOTES.md" ($assets + "SHA256SUMS.txt" | ForEach-Object { "$out\$_" })
