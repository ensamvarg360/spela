import os
import threading
from pathlib import Path

from app import exfat, lz4pack


def _game(root: Path, ampr: bool = True) -> Path:
    for rel, data in {"sce_sys/param.json": b'{"titleId": "PPSA01234"}',
                      "eboot.bin": b"\x7fELF" + (b"libSceAmpr.sprx" if ampr else b"") + os.urandom(64),
                      "data/level.assets": b"forest " * 300_000,  # compresses well: packed
                      "movie/intro.mp4": os.urandom(4096)}.items():  # video: always a plain file
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    return root


def test_lz4_pack(tmp_path):
    """A game that uses AMPR is packed and checked; only its assets leave as plain files, and the emulator comes
    along. The folder and the .exfat get the same files."""
    src, out, stop = _game(tmp_path / "game"), tmp_path / "out", threading.Event()
    assert "doesn't use AMPR" in lz4pack.blocker(_game(tmp_path / "plain", ampr=False))
    assert lz4pack.blocker(src) is None

    packed = lz4pack.pack(src, out, lambda d, t: None, stop, lambda m: None)
    assert packed == {"data/level.assets"}
    assert (out / "fakelib" / "libSceAmpr.sprx").read_bytes() == lz4pack.SPRX.read_bytes()
    assert sum(p.stat().st_size for p in out.glob("ampr_assets-*.pak")) < 300_000  # from 2.1 MB (64 KiB pages)

    image = exfat._walk([str(src), str(out)], "", 0, frozenset(packed))
    lz4pack.copy_loose(src, out, packed, stop, lambda m: None)
    folder = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert {"eboot.bin", "sce_sys/param.json", "movie/intro.mp4", "fakelib/libSceAmpr.sprx", lz4pack.MARK} <= folder
    assert "data/level.assets" not in folder

    def files(node, rel=""):
        return [f for c in node.children for f in (files(c, rel + c.name + "/") if c.is_dir else [rel + c.name])]
    assert set(files(image)) == folder
    assert lz4pack.blocker(out) == "This game is already LZ4-packed."
