import os
import struct
from threading import Event

import pytest

from app import exfat
from app.consoles import AdapterError


def _read_tree(img: bytes) -> dict[str, bytes | None]:
    """Reads an image back the way an exFAT driver would: checksums, FAT chains, entry sets. Directories map to None."""
    assert img[3:11] == b"EXFAT   " and img[510:512] == b"\x55\xAA"
    assert img[:6144] == img[6144:12288], "backup boot region"
    stored = struct.unpack_from("<I", img, 11 * 512)[0]
    assert stored == exfat._sum32(img[:11 * 512], skip=(106, 107, 112))
    vol, fat_off, _, heap, count, root = struct.unpack_from("<QIIIII", img, 72)
    assert vol * 512 == len(img)
    fat = struct.unpack_from(f"<{count + 2}I", img, fat_off * 512)
    spc = 1 << img[109]

    def chain(first: int) -> bytes:
        out, c = b"", first
        while c != exfat.EOC:
            assert 2 <= c < count + 2
            out += img[(heap + (c - 2) * spc) * 512:][:spc * 512]
            c = fat[c]
        return out

    upcase = None
    tree = {}

    def walk(data: bytes, prefix: str):
        nonlocal upcase
        i = 0
        while i < len(data) and data[i]:
            kind = data[i]
            if kind == 0x82:
                checksum, first, length = struct.unpack_from("<4xI12xIQ", data, i)
                table = chain(first)[:length]
                assert exfat._sum32(table) == checksum
                upcase = exfat._expand(table)
            if kind != 0x85:
                i += 32
                continue
            n = data[i + 1] + 1
            entry_set = data[i:i + n * 32]
            assert struct.unpack_from("<H", entry_set, 2)[0] == exfat._sum16(entry_set, skip=(2, 3))
            attr = struct.unpack_from("<H", entry_set, 4)[0]
            _, flags, _, name_len, name_hash, _, valid, _, first, length = struct.unpack_from("<BBBBHHQIIQ", entry_set, 32)
            name = b"".join(entry_set[k * 32 + 2:k * 32 + 32] for k in range(2, n))[:name_len * 2].decode("utf-16-le")
            up = struct.pack(f"<{name_len}H", *(upcase[u] for u in struct.unpack(f"<{name_len}H", name.encode("utf-16-le"))))
            assert exfat._sum16(up) == name_hash and valid == length
            content = chain(first)[:length] if first else b""
            if attr & 0x10:
                tree[prefix + name] = None
                walk(content, prefix + name + "/")
            else:
                tree[prefix + name] = content
            i += n * 32

    walk(chain(root), "")
    assert set(fat[2:]) <= set(range(3, count + 2)) | {exfat.EOC}
    return tree


def test_upcase_table_is_the_standard_one():
    assert exfat._sum32(exfat.UPCASE) == exfat.UPCASE_CHECKSUM and len(exfat.UPCASE) == 5836
    assert exfat.UPCASE_MAP[ord("a")] == ord("A") and exfat.UPCASE_MAP[ord("å")] == ord("Å") and len(exfat.UPCASE_MAP) == 0x10000


def test_build_round_trip(tmp_path):
    src = tmp_path / "PPSA01234"
    files = {
        "eboot.bin": os.urandom(100_000),
        "sce_sys/param.json": b'{"titleId": "PPSA01234"}',
        "sce_sys/empty.dat": b"",
        "sce_module/libc.prx": b"x" * exfat.CLUSTER,  # exactly one cluster
        "Data/A very long file name that needs more than one name entry, åäö.bin": b"long",
    }
    for rel, data in files.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(data)
    (src / "empty_dir").mkdir()
    for i in range(1100):  # more entries than fit in one directory cluster
        (src / "many" / f"f{i:04}").parent.mkdir(exist_ok=True)
        (src / "many" / f"f{i:04}").write_bytes(bytes([i % 256]))
    seen = []

    dest = tmp_path / "PPSA01234.exfat"
    size = exfat.build(src, dest, lambda d, t: seen.append((d, t)), Event(), "PPSA01234")

    img = dest.read_bytes()
    assert size == len(img) and not (tmp_path / "PPSA01234.exfat.building").exists()
    tree = _read_tree(img)
    for rel, data in files.items():
        assert tree[rel] == data
    assert tree["empty_dir"] is None and tree["many/f1099"] == bytes([1099 % 256])
    assert len([k for k in tree if k.startswith("many/")]) == 1100
    assert seen[-1][0] == seen[-1][1] == sum(len(d) for d in files.values()) + 1100


def test_cancel_removes_partial_image(tmp_path):
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "eboot.bin").write_bytes(b"x" * 10)
    stop = Event()
    stop.set()
    with pytest.raises(AdapterError):
        exfat.build(tmp_path / "g", tmp_path / "g.exfat", lambda d, t: None, stop)
    assert list(tmp_path.iterdir()) == [tmp_path / "g"]
