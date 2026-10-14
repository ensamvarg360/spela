# Builds backend\app\payload\storefront-helper.elf in a Linux container. Needs Docker Desktop.
# Run it after changing main.c or the app version; the built .elf is committed, so ../build.ps1 doesn't need Docker.
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$sdk = "https://github.com/ps5-payload-dev/sdk/releases/download/v0.44/ps5-payload-sdk.zip"
$steps = "apt-get update -qq && apt-get install -y -qq clang-18 lld-18 make wget unzip > /dev/null" +
    " && wget -q -O /tmp/sdk.zip $sdk && unzip -q -d /opt /tmp/sdk.zip && PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk make -B"
docker run --rm -v "$PSScriptRoot\..:/src" -w /src/payload ubuntu:24.04 bash -c $steps
