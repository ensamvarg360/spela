#!/usr/bin/env bash
# Checks the e2a packer (payload/e2a) the way the helper drives it (pack_thread in payload/main.c), on Linux:
# drakmor's Python tools (backend/app/ampr) must read its packs back byte for byte. From the repo root:
#   docker run --rm -v "${PWD}:/repo:ro" ubuntu:24.04 bash /repo/payload/hosttest/run.sh
set -euo pipefail
apt-get update -qq && apt-get install -y -qq gcc make python3 python3-pip wget > /dev/null
pip3 install -q --break-system-packages lz4
mkdir -p /tmp/lz4 && for f in lz4.c lz4.h lz4hc.c lz4hc.h; do wget -q -O /tmp/lz4/$f https://raw.githubusercontent.com/lz4/lz4/v1.10.0/lib/$f; done
rm -rf /tmp/t && cp -r /repo/payload/hosttest /tmp/t && cd /tmp/t
gcc -O2 -std=gnu11 -DE2A_HOST -D_GNU_SOURCE -I/repo/payload/e2a -I/tmp/lz4 -o packer driver.c \
  /repo/payload/e2a/{scan,ampr_pack,ampr_index,verify,util,sha256}.c /tmp/lz4/lz4.c /tmp/lz4/lz4hc.c -lpthread -lm
rm -rf game work && mkdir -p game/sce_sys game/data game/movie game/sce_module
echo '{"titleId":"PPSA01234","contentVersion":"01.000.000","localizedParameters":{"defaultLanguage":"en-US","en-US":{"titleName":"Test"}}}' > game/sce_sys/param.json
printf '\x7fELF....libSceAmpr.sprx....' > game/eboot.bin
python3 -c "
import os
open('game/data/level.assets','wb').write(b'forest ' * 3_000_000)
open('game/data/textures.bin2','wb').write(os.urandom(1<<20) + b'stone ' * 2_000_000)
open('game/movie/intro.mp4','wb').write(os.urandom(300000))
"
mkdir work && ./packer game work /repo/backend/app/assets/libSceAmpr.sprx
cd /repo/backend && python3 - <<'PY'
from pathlib import Path
from app.ampr import ampr_pack as ap
work, game = Path("/tmp/t/work"), Path("/tmp/t/game")
print("verify vs original:", ap.verify_packs_against_root(work / "ampr_assets.index", game))
for f in ap.list_manifest(work / "ampr_assets.index"):
    print(f["path"], "packed" if f["packed"] else "loose", f["logical_size"], "->", f["stored_size"])
print(sorted(str(p.relative_to(work)) for p in work.rglob("*") if p.is_file()))
PY
