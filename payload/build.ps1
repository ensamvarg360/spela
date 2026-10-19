# Builds backend\app\payload\spela.elf in a Linux container. Needs Docker Desktop.
# Run it after changing main.c or the app version; the built .elf is committed, so ../build.ps1 doesn't need Docker.
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$sdk = "https://github.com/ps5-payload-dev/sdk/releases/download/v0.44/ps5-payload-sdk.zip"
$sqlite = "https://www.sqlite.org/2024/sqlite-amalgamation-3460100.zip"
$sqliteSha256 = "77823cb110929c2bcb0f5d48e4833b5c59a8a6e40cdea3936b99e199dbbe5784"
$steps = "apt-get update -qq && apt-get install -y -qq clang-18 lld-18 make wget unzip > /dev/null" +
    " && wget -q -O /tmp/sdk.zip $sdk && unzip -q -d /opt /tmp/sdk.zip" +
    " && wget -q -O /tmp/sqlite.zip $sqlite && echo '$sqliteSha256  /tmp/sqlite.zip' | sha256sum -c --quiet" +
    " && unzip -q -j -d /tmp/sqlite /tmp/sqlite.zip && PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk make -B && rm -f sqlite3.o"
docker run --rm -v "$PSScriptRoot\..:/src" -w /src/payload ubuntu:24.04 bash -c $steps
