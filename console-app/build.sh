#!/usr/bin/env bash
# Runs inside the Linux container that build.ps1 starts. The repository is at /repo.
#   build.sh app      builds the PS5 app into backend/app/console_app/<TITLE_ID>
#   build.sh preview  renders the screen on this PC into console-app/preview/, against a fake PC app
#
# The app is ps5-homebrew-ui (GPL-3.0-or-later) at a pinned commit with our files laid over it:
# its build, renderer and widgets; our screen (src/concepts) and network client (src/store).
set -euo pipefail
mode=${1:-app}
KIT=https://github.com/blackbearreloaded/ps5-homebrew-ui
COMMIT=bbe19054ee2e7ec069254775273aeb8846c4bd3e
TITLE_ID=PPSA99787 # also CONSOLE_APP_TITLE_ID in backend/app/consoles.py

packages=(clang-18 lld-18 llvm-18 make ninja-build ccache pkg-config python3 curl wget git unzip xz-utils ca-certificates file)
[[ $mode == preview ]] && packages+=(libegl-dev libgl-dev libegl-mesa0 libgl1-mesa-dri)
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${packages[@]}" >/dev/null

kit=/work/kit
git clone -q "$KIT" "$kit"
git -C "$kit" checkout -q "$COMMIT"
cd "$kit"
mkdir -p /deps/kit
ln -s /deps/kit .deps # the SDKs (~300 MB) stay in a Docker volume between builds

# Our screen replaces the kit's 21 demo designs.
rm -rf src/concepts
cp -r /repo/console-app/src/. src/
# The store client turns covers into textures, so it needs the renderer's batch.
for f in src/main.cpp host/snapshot_main.cpp; do
    grep -q '^ *\(hui::\)\{0,1\}demo::Catalog catalog;' "$f"
    sed -i 's|^\( *\)\(hui::\)\{0,1\}demo::Catalog catalog;|\1hui::store::set_texture_batch(\&renderer.batch());\n&|' "$f"
    sed -i '0,/^#include "app\/shell.hpp"/s||#include "store/client.hpp"\n&|' "$f"
done

# Leave out what we don't use: music, the second sound set, the kit's home-screen art.
rm -rf assets/audio/music assets/audio/sfx/paper
rm -f sce_sys/pic0.dds sce_sys/pic1.dds sce_sys/snd0.at9 sce_sys/*-source.png
cp /repo/console-app/sce_sys/icon0.png sce_sys/icon0.png
TITLE_ID=$TITLE_ID APP_NAME="PS5 Local Storefront" make init >/dev/null

if [[ $mode == preview ]]; then
    printf 'url=http://127.0.0.1:8799\ntoken=preview\n' >/tmp/server.txt
    python3 /repo/console-app/preview_server.py 8799 &
    sleep 1
    rm -rf /repo/console-app/preview
    HUI_SERVER_FILE=/tmp/server.txt HOST_CXX=clang++-18 HOST_CC=clang-18 \
        tools/host-snapshots.sh /repo/console-app/preview storefront
    exit
fi

make
out=/repo/backend/app/console_app
rm -rf "${out:?}/$TITLE_ID"
mkdir -p "$out"
cp -a "dist/$TITLE_ID" "$out/$TITLE_ID"
echo "Built $out/$TITLE_ID"
