"""LZ4-packs a folder dump for drakmor's AMPR emulator (ampr/), the format exFAT2AMPR makes on the console: the game's
assets go into ampr_assets-NNN.pak volumes (64 KiB LZ4 blocks, identical blocks stored once), and
fakelib/libSceAmpr.sprx, which ShadowMountPlus loads into the game, unpacks them as the game reads them. Only games
that read their assets through the PS5's AMPR library can run like this, and even some of those don't (Unity games),
so the original is always kept until the user deletes it."""
import contextlib
import io
import os
import shutil
from pathlib import Path
from threading import Event

from .ampr import ampr_pack as ap
from .ampr import build_ampr_index
from .ampr.ampr_pack_format import asset_relative_path
from .consoles import AdapterError, ProgressCallback

SPRX = Path(__file__).parent / "assets" / "libSceAmpr.sprx"
MARK = "ampr_assets.index"  # in a packed game's root
# Kept as plain files, as exFAT2AMPR leaves them (its docs/TECHNIQUE.md): executables and modules, system folders, and
# files that are already compressed or that games read some other way. `*` also matches "/".
LOOSE = ("eboot.bin", "*.prx", "*.sprx", "*.elf", "*.self", "sce_sys/*", "sce_module/*", "fakelib/*", "fakelib2/*",
         "system/*", "mods/*", "save/*", "trophy2/*", "uds/*", "_DUPLEX_/*", "*.mp4", "*.bk2", "*.usm", "*.webm",
         "*.at9", "*.ogg", "*.mp3", "*.json", "*.xml", "*.utoc", "*.bin", "*.dat", "*.zip", "*.7z", "*.rar", "*.pak")
CONFIG = ap.BuildConfig(default_action="compress", auto_loose_min_file_size=16 * 1024**2,  # incompressible: loose from 16 MB
                        compression_level=9,  # ~7x faster than its default 12 for about the same size; unpacking is as fast
                        groups={"default": ap.GroupConfig("default")},
                        rules=[ap.Rule("loose", include=LOOSE + tuple(p.upper() for p in LOOSE))])


def blocker(folder: Path) -> str | None:
    """Why the folder dump can't be LZ4-packed, or None."""
    if (folder / MARK).exists():
        return "This game is already LZ4-packed."
    modules = [folder / "eboot.bin", *(folder / "sce_module").glob("*")]
    try:
        if not any(b"libSceAmpr" in p.read_bytes() for p in modules if p.is_file()):
            return ("This game doesn't use AMPR, so LZ4 can't be used for it (or its eboot.bin is still encrypted, "
                    "and Spela can't tell).")
    except OSError as e:
        return f"Couldn't read the game's executable: {e.strerror or e}"
    return None


def pack(src: Path, out: Path, progress: ProgressCallback, stop: Event, say) -> set[str]:
    """Writes the packs, their index and the emulator into `out` and checks every packed file against `src`, byte for
    byte. Returns the packed files (relative paths), which the game no longer needs as plain files. `say` gets what's
    happening, for the progress message."""
    out.mkdir(parents=True, exist_ok=True)
    index = out / "ampr_emu.index"

    def step(p: ap.BuildProgress):
        if stop.is_set():
            raise AdapterError("canceled", "Canceled.")
        if p.phase == "packing":
            progress(p.logical_bytes_done, p.logical_bytes_total)

    sprx = out / "fakelib" / SPRX.name
    sprx.parent.mkdir(exist_ok=True)
    shutil.copyfile(SPRX, sprx)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:  # its CLI output
        say("Listing the game's files.")
        if not _write_index(src, sprx, index):
            raise AdapterError("pack_failed", f"Couldn't list the game's files: {err.getvalue().strip()}")
        say("Packing with LZ4.")
        try:
            manifest, stats, _ = ap.build_packs(root=src, ampr_index=index, output_dir=out, config=CONFIG, progress=step)
            if not stats.files_packed:
                raise AdapterError("nothing_to_pack", "Nothing in this game gets smaller with LZ4.")
            say("Checking every packed file against the original.")
            ap.verify_packs_against_root(manifest, src)  # raises on the first difference
        except (ap.PackError, ValueError) as e:
            raise AdapterError("pack_failed", f"Couldn't pack the game: {e}")
    return {asset_relative_path(f["path"]) for f in ap.list_manifest(manifest) if f["packed"]}


def _write_index(src: Path, sprx: Path, index: Path) -> bool:
    """ampr_emu.index of the game as it will be: src's files, with the emulator in fakelib (the game's own copy, if it
    had one, replaced), as build_ampr_index.build_index_local and exFAT2AMPR list them. False on a path clash."""
    rows, seen, emu = [], {}, f"/app0/fakelib/{SPRX.name}"
    for dirpath, dirs, names in os.walk(src):
        dirs.sort(key=build_ampr_index.key_for)
        for name in sorted(names, key=build_ampr_index.key_for):
            path = Path(dirpath, name)
            rel = build_ampr_index.app0_path(src, path)
            if rel.lower() in (emu.lower(), "/app0/ampr_commands.bin", "/app0/apr_emu.log") or not path.is_file():
                continue
            st = path.stat()
            if not build_ampr_index.validate_and_add_row(rows, seen, st.st_size, int(st.st_mtime), rel, False):
                return False
    st = sprx.stat()
    if not build_ampr_index.validate_and_add_row(rows, seen, st.st_size, int(st.st_mtime), emu, False):
        return False
    build_ampr_index.write_index(rows, index)
    return True


def copy_loose(src: Path, out: Path, packed: set[str], stop: Event, say):
    """Copies every file of `src` the packs don't hold into `out`, for an LZ4 folder. What `out` already has (the
    emulator) wins."""
    say("Copying the files that stay as they are.")
    for dirpath, _, names in os.walk(src):
        for name in names:
            rel = Path(dirpath, name).relative_to(src).as_posix()
            if rel in packed or (out / rel).exists():
                continue
            if stop.is_set():
                raise AdapterError("canceled", "Canceled.")
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(dirpath, name), out / rel)  # ponytail: per file, so cancel waits out one big video
