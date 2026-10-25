# e2a: the LZ4 packer from exFAT2AMPR (modified)

**Based on exFAT2AMPR by V-10. Made by V-10.** This folder is a modified copy of part of
[exFAT2AMPR](https://github.com/exFAT2AMPR/exfat2ampr), commit `6b3a76a12e504c196848d49e2676047fc2a8f5d4`:
`scan`, `ampr_pack`, `ampr_index`, `verify`, `util`, `sha256` and their headers. It is GPL-3.0-or-later with
V-10's additional terms in [NOTICE](NOTICE), which apply to this folder. This copy is not exFAT2AMPR, and V-10
didn't make or endorse Spela.

Spela's helper (`../main.c`, `pack_start()`) uses it to LZ4-pack games on the console. Modified by ensamvarg360 for
Spela:

- `common.h`: `E2A_STATE_DIR` can be set when building, so the packer writes its log to `/data/spela/lz4` (see
  `../Makefile`) instead of exFAT2AMPR's own folder.

Nothing else is changed. The rest of exFAT2AMPR (its app, web page, job runner, game mode, plugins) isn't used:
`activity_workers()` and `activity_throttle()`, which `activity.h` declares, are Spela's, in `../main.c`.
To update: copy the same files from a newer exFAT2AMPR commit, put the `common.h` change back, update the commit
above, and check that the host test still passes (`payload/hosttest/run.sh`, with Docker).
