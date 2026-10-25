# Builds backend\app\payload\spela.elf in a Linux container. Needs Docker Desktop.
# Run it after changing main.c or the app version; the built .elf is committed, so ../build.ps1 doesn't need Docker.
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $true
$sdk = "https://github.com/ps5-payload-dev/sdk/releases/download/v0.44/ps5-payload-sdk.zip"
$sqlite = "https://www.sqlite.org/2024/sqlite-amalgamation-3460100.zip"
$sqliteSha256 = "77823cb110929c2bcb0f5d48e4833b5c59a8a6e40cdea3936b99e199dbbe5784"
# lz4 for the LZ4 packer (e2a/): the version exFAT2AMPR and the AMPR emulator use
$lz4 = "https://raw.githubusercontent.com/lz4/lz4/v1.10.0/lib"
$lz4Sha256 = @{
    "lz4.c" = "9396f7de527bc8435de9c7569fb7998e56545a84b4f3c2d808c0235c01774539"
    "lz4.h" = "26b82efc53d1570f3b54eef02e9c4764c1ad374ff03cac04e2ced5ea4d4c552f"
    "lz4hc.c" = "126cafafdb91767e6e55238298a910903851b35b2cee27ce80ae2280469ee232"
    "lz4hc.h" = "e43824e8a9ba16f54100c4ccbccfa5782a858ca9ab83c48aac303fea3e76e21e"
}
$getLz4 = ($lz4Sha256.Keys | ForEach-Object { "wget -q -O /tmp/lz4/$_ $lz4/$_ && echo '$($lz4Sha256[$_])  /tmp/lz4/$_' | sha256sum -c --quiet" }) -join " && "
$steps = "apt-get update -qq && apt-get install -y -qq clang-18 lld-18 make wget unzip xxd > /dev/null" +
    " && wget -q -O /tmp/sdk.zip $sdk && unzip -q -d /opt /tmp/sdk.zip" +
    " && wget -q -O /tmp/sqlite.zip $sqlite && echo '$sqliteSha256  /tmp/sqlite.zip' | sha256sum -c --quiet" +
    " && unzip -q -j -d /tmp/sqlite /tmp/sqlite.zip && mkdir -p /tmp/lz4 && $getLz4" +
    " && PS5_PAYLOAD_SDK=/opt/ps5-payload-sdk make -B; status=`$?; rm -f *.o e2a/*.o sprx.c; exit `$status"
docker run --rm -v "$PSScriptRoot\..:/src" -w /src/payload ubuntu:24.04 bash -c $steps
