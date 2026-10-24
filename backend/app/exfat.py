"""Builds a raw .exfat disc image (no partition table) from a folder dump, so ShadowMountPlus can mount the game as
one file. Written from Microsoft's exFAT specification, in plain Python: no driver, admin rights or WSL needed.
The folder's contents (sce_sys, eboot.bin, ...) sit at the image's root. Every file and directory gets one contiguous
run of clusters, and its FAT chain is written out too, so readers that ignore the NoFatChain shortcut work as well."""
import base64
import os
import shutil
import struct
import time
import zlib
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event

from .consoles import AdapterError, ProgressCallback

SECTOR = 512
# ponytail: one cluster size for every game (Windows' exFAT default up to 32 GB). Wastes ~16 KB per small file;
# make it a parameter if PS5 load times or image sizes say a bigger or smaller one is better.
CLUSTER = 32 * 1024
SPC = CLUSTER // SECTOR  # sectors per cluster
FAT_OFFSET = 2048  # sectors: the FAT starts 1 MiB in, as mkfs.exfat lays it out
ENTRY = 32
BUILDING = ".building"  # suffix while the image is written; the scanner ignores it
EOC = 0xFFFFFFFF  # end of a FAT chain


@dataclass
class _Node:
    path: str
    name: str
    is_dir: bool
    size: int = 0  # file: its bytes; directory: its entries' bytes
    mtime: float = 0.0
    children: list["_Node"] = field(default_factory=list)
    first: int = 0  # first cluster, 0 for an empty file
    clusters: int = 0


def _clusters(n: int) -> int:
    return -(-n // CLUSTER)


def _name_entries(name: str) -> int:
    units = len(name.encode("utf-16-le")) // 2
    if units > 255:
        raise AdapterError("name_too_long", f"{name} is longer than exFAT allows (255 characters).")
    return 2 + -(-units // 15)  # file + stream extension + one entry per 15 name characters


def _walk(paths: list[str], name: str, mtime: float, skip: frozenset[str] = frozenset(), rel: str = "") -> _Node:
    """The folders in `paths` laid over each other (a file in a later one wins), without the files in `skip`."""
    node = _Node(paths[0], name, True, mtime=mtime)
    entries: dict[str, list[os.DirEntry]] = {}
    for path in paths:
        with os.scandir(path) as it:
            for e in it:
                entries.setdefault(e.name, []).append(e)
    for n in sorted(entries):
        es = entries[n]
        if rel + n in skip:
            continue
        for e in es:
            if e.is_symlink() or e.is_junction():
                raise AdapterError("unsafe_link", f"{e.path} is a link. Links can't go into an image.")
        if es[-1].is_dir():
            child = _walk([e.path for e in es if e.is_dir()], n, es[-1].stat().st_mtime, skip, rel + n + "/")
        else:
            st = es[-1].stat()
            child = _Node(es[-1].path, n, False, st.st_size, st.st_mtime)
        node.children.append(child)
        node.size += _name_entries(n) * ENTRY
    return node


def _expand(table: bytes) -> list[int]:
    """The compressed up-case table as a full map: 0xFFFF, n means the next n code units map to themselves
    (except as the very last entry, U+FFFF's own mapping)."""
    out, it = [], iter(struct.unpack(f"<{len(table) // 2}H", table))
    for v in it:
        out += range(len(out), len(out) + next(it)) if v == 0xFFFF and len(out) < 0xFFFF else [v]
    return out + list(range(len(out), 0x10000))


def _sum16(data: bytes, skip=()) -> int:
    c = 0
    for i, b in enumerate(data):
        if i not in skip:
            c = ((c << 15 | c >> 1) + b) & 0xFFFF
    return c


def _sum32(data: bytes, skip=()) -> int:
    c = 0
    for i, b in enumerate(data):
        if i not in skip:
            c = ((c << 31 | c >> 1) + b) & 0xFFFFFFFF
    return c


def _stamp(t: float) -> tuple[int, int]:
    """(timestamp, 10 ms increment) in UTC; the entries' UTC offset fields say so."""
    t = min(max(t, 315532800), 4354819198)  # exFAT covers 1980 to 2107
    tm = time.gmtime(t)
    stamp = (tm.tm_year - 1980) << 25 | tm.tm_mon << 21 | tm.tm_mday << 16 | tm.tm_hour << 11 | tm.tm_min << 5 | tm.tm_sec // 2
    return stamp, tm.tm_sec % 2 * 100 + int(t % 1 * 100)


def _entry_set(node: _Node, upcase: list[int]) -> bytes:
    name16 = node.name.encode("utf-16-le")
    units = struct.unpack(f"<{len(name16) // 2}H", name16)
    name_hash = _sum16(struct.pack(f"<{len(units)}H", *(upcase[u] for u in units)))
    chunks = [name16[i:i + 30] for i in range(0, len(name16), 30)]
    stamp, ms = _stamp(node.mtime)
    length = node.clusters * CLUSTER if node.is_dir else node.size
    data = bytearray(struct.pack("<BBHHHIIIBBBBB7x", 0x85, 1 + len(chunks), 0, 0x10 if node.is_dir else 0x20, 0,
                                 stamp, stamp, stamp, ms, ms, 0x80, 0x80, 0x80))  # 0x80: UTC, offset 0
    data += struct.pack("<BBBBHHQIIQ", 0xC0, 0x01, 0, len(units), name_hash, 0, length, 0, node.first, length)
    for chunk in chunks:
        data += struct.pack("<BB30s", 0xC1, 0, chunk)
    struct.pack_into("<H", data, 2, _sum16(data, skip=(2, 3)))
    return bytes(data)


def _boot_region(volume_sectors: int, fat_sectors: int, heap_offset: int, count: int, root: int) -> bytes:
    boot = bytearray(SECTOR)
    boot[0:11] = b"\xEB\x76\x90EXFAT   "
    struct.pack_into("<QQIIIIIIHHBBBBB", boot, 64, 0, volume_sectors, FAT_OFFSET, fat_sectors, heap_offset, count, root,
                     int(time.time()) & 0xFFFFFFFF, 0x0100, 0, SECTOR.bit_length() - 1, SPC.bit_length() - 1, 1, 0x80, 100)
    boot[120:510] = b"\xF4" * 390  # boot code: halt
    boot[510:512] = b"\x55\xAA"
    extended = bytes(SECTOR - 4) + b"\x00\x00\x55\xAA"
    region = bytes(boot) + extended * 8 + bytes(SECTOR) * 2  # + OEM parameters and a reserved sector
    return region + struct.pack("<I", _sum32(region, skip=(106, 107, 112))) * (SECTOR // 4)


def _layout(root: _Node) -> tuple[int, int, int, list[_Node], list[_Node], list[int]]:
    """Assigns clusters: bitmap, up-case table, every directory, then every file's data.
    Returns (volume sectors, FAT sectors, cluster count, directories, files, [bitmap first, up-case first, bitmap bytes])."""
    root.size += 3 * ENTRY  # volume label, allocation bitmap, up-case table
    dirs, files, stack = [], [], [root]
    while stack:
        d = stack.pop()
        dirs.append(d)
        d.clusters = max(1, _clusters(d.size))
        for c in d.children:
            if c.is_dir:
                stack.append(c)
            else:
                c.clusters = _clusters(c.size)
                files.append(c)
    upcase_clusters = _clusters(len(UPCASE))
    rest = upcase_clusters + sum(n.clusters for n in dirs + files)
    bitmap_clusters = 1
    while _clusters(-(-(rest + bitmap_clusters) // 8)) > bitmap_clusters:
        bitmap_clusters += 1
    count = rest + bitmap_clusters
    if count > 0xFFFFFFF5:
        raise AdapterError("too_large", "The game is too large for one exFAT image.")
    nxt = 2 + bitmap_clusters + upcase_clusters
    for n in dirs + files:
        if n.clusters:
            n.first, nxt = nxt, nxt + n.clusters
    fat_sectors = -(-(count + 2) * 4 // SECTOR)
    heap = -(-(FAT_OFFSET + fat_sectors) // SPC) * SPC
    return heap + count * SPC, fat_sectors, count, dirs, files, [2, 2 + bitmap_clusters, -(-count // 8)]


def build(src: Path, dest: Path, progress: ProgressCallback, stop: Event, label: str = "",
          over: Path | None = None, skip: frozenset[str] = frozenset()) -> int:
    """Writes the image to dest (via dest.building, renamed when complete). Returns its size in bytes. `over`: a
    folder whose files go in too, replacing src's of the same name; `skip`: src's files (relative paths) left out."""
    root = _walk([str(src)] + ([str(over)] if over else []), "", os.stat(src).st_mtime, skip)
    volume_sectors, fat_sectors, count, dirs, files, (bitmap_first, upcase_first, bitmap_len) = _layout(root)
    size = volume_sectors * SECTOR
    tmp = dest.with_name(dest.name + BUILDING)
    free = shutil.disk_usage(dest.parent).free + (tmp.stat().st_size if tmp.exists() else 0)
    if free < size:
        raise AdapterError("no_space", f"Not enough space next to the folder: the image needs {size / 1024**3:.1f} GiB, "
                                       f"{free / 1024**3:.1f} GiB is free.")
    heap = volume_sectors - count * SPC

    fat = array("I", range(1, count + 3))  # contiguous runs: each cluster points at the next
    fat[0], fat[1] = 0xFFFFFFF8, EOC
    runs = [(bitmap_first, upcase_first - bitmap_first), (upcase_first, _clusters(len(UPCASE)))]
    for first, n in runs + [(x.first, x.clusters) for x in dirs + files]:
        if n:
            fat[first + n - 1] = EOC
    bitmap = b"\xFF" * (count // 8) + (bytes([(1 << count % 8) - 1]) if count % 8 else b"")  # every cluster is used

    label16 = label[:11].encode("utf-16-le")
    root_entries = struct.pack("<BB22s8x", 0x83, len(label16) // 2, label16)
    root_entries += struct.pack("<BB18xIQ", 0x81, 0, bitmap_first, bitmap_len)
    root_entries += struct.pack("<B3xI12xIQ", 0x82, UPCASE_CHECKSUM, upcase_first, len(UPCASE))

    total, done = sum(x.size for x in files), 0
    try:
        with open(tmp, "wb") as f:
            def at(cluster: int, data: bytes):
                f.seek((heap + (cluster - 2) * SPC) * SECTOR)
                f.write(data)

            f.write(_boot_region(volume_sectors, fat_sectors, heap, count, root.first) * 2)  # main and backup
            f.seek(FAT_OFFSET * SECTOR)
            f.write(fat.tobytes())
            at(bitmap_first, bitmap)
            at(upcase_first, UPCASE)
            for d in dirs:
                at(d.first, (root_entries if d is root else b"") + b"".join(_entry_set(c, UPCASE_MAP) for c in d.children))
            buf = memoryview(bytearray(8 * 1024**2))
            for x in files:
                if not x.size:
                    continue
                f.seek((heap + (x.first - 2) * SPC) * SECTOR)
                left = x.size
                with open(x.path, "rb") as src_file:
                    while left:
                        if stop.is_set():
                            raise AdapterError("canceled", "Canceled.")
                        n = src_file.readinto(buf[:min(left, len(buf))])
                        if not n:
                            break
                        f.write(buf[:n])
                        left, done = left - n, done + n
                        progress(done, total)
                    if left or src_file.read(1):
                        raise AdapterError("source_changed", f"{x.path} changed while it was being copied. Try again.")
            f.truncate(size)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    progress(total, total)
    return size


# The specification's recommended up-case table, in its compressed form (5836 bytes, checksum 0xE619D30D), as
# mkfs.exfat and Windows write it; zlib + base64 here.
_UPCASE_Z = (
    "eNqt2HXQFseWBvCe7hlc3uDWb5+GF280uLsHCxAcgoUQIIQQQgjB3d3d3Z0P+HB3d0IIIQR3P/uQ3dqqe9mquza/6j+emjoz3dPVY0J4QgolfBGIGCKm"
    "iCViizgirogn4osEIqEIiU9EIpFYJBFJRTKRXKQQKUUqkVqkEVqEhREkrEgr0omISC8yiIwik8gssoiswolsIrvIIXKKXCK3+FTkEXlFPpFfFBAFRSFR"
    "WBQRRUUxUVyUECVFKVFalBFlRTlRXlQQFUUlUVlUEZ+JqqKaqC5qiJric1FL1BZ1xBeirqgn6osGoqFoJBqLJqLp/6r+Z9FN/CK6ix6ip+gleos+oq/o"
    "J/qLAWKgGCQGiyFiqBgmhosRYqQYJUaLMWKsGCfGiwliopgkJospYqqYJqaLGWKmmCVmizlirpgn5osFYqFYJBaLJWKpWCaWixVipVglVos1Yq1YJ9aL"
    "DWKj2CQ2iy0iSmwV28R2ES12iJ1il9gt9oi9Yp/YLw6Ig+KQOCyOiKPimDguToiT4pQ4Lc6Is+KcOC8uiIvikrgsroir4tr/sP7FP9V38TD9ngQfYkAs"
    "iAPxIAGEIBEkgWSQAlJBGggDQVqIQAbIBFnAQXbI6eXyckMeyAcFvIJQGIpCcSgJpaEslIeKXiWoAlWhOtSEWlAH6kJ9aAiNoSk0gxbQClpDG2gL7aED"
    "dIRO0Bm6eD/Bz/AL9PBKy15eb+gL/b0BMMgb7A2BYd5wb4Q30hsFY7yx3nNvvDfBmwhF5RRvqjfNIznDmwmzYS7M9xbAIm+xt8RbCsu9FbDKW+2tgXWw"
    "wdsIm70tXhRs81540d4Ob6e3y9vt7UHb5+1HO+gdQjsCx+AEnIIzcA4uwCUY7l2F63ADbsItuA134C7ch4feI+8x2lN4jvO9hNfwFt4DFr/8sPkQA2JB"
    "HIgHCSAEiSAJJIMUkArSQBhIWpkWIpABMkEWcJAdckJuyCPzynwyvywgC8qWrrAsLIvKVq64LCFLQmlZRpaV5aACVIIqUBWqyxqyJuaov1dbfpiZunKE"
    "V1+O9BrKRrKxbCLHeF/KZnKs10K2lK3kV3ICZqmNbObaynayvZzqdZDfyWne97IT5qqz/FF2kT/JrvJn2U22cN1lDznf6yV7y0VeX9lP9pcD5HKvjPww"
    "Y2XlMDlcjpAj5Si5wRsjx8pxcrycICfKSXKynCKnymlyupwhZ8pZcracI+fKeXK+XCAXykVysVwil8plcrlcIVfKVXK1XCPXynVyvdwgN8pNcrPcIqPk"
    "VrlNbpfRcofcKXfJ3XKP3Cv3yf3ygDwoD8nD8og8Ko/J4/KEPClPydPyjDwrz8nz8oK8KC/Jy/KKvCqvyevyV3lD/iZvyt/lLfmHvC3/lHfkX/KuvCfv"
    "ywfyoXwkH8sn8ql8Jp/LF/KlfCVfyzfyrXwn30vGrd9TUinlq0DFUDFVLBVbxVFxVTwVXyVQCVVIfaISqcQqiUqqkqnkKoVKqVKp1CqN0iqsjCJlVVqV"
    "TkVUepVBZVSZVGaVRWVVTmVT2VUOlVPlUrnVpyqPyqvyqfyqgCqoCqnCqogqqoqp4qqEKqlKqdKqjCqryqnyqoKqqCqpyqqK+kxVVdVUdVVD1VSfq1qq"
    "tqqjvlB1VT1VXzVQDVUj1Vg1UU3Vl6qZaq5aqJaqlfpKtVZfqzbqG9VWtVPt1beqg/pOdVTfq07qB9VZ/ai6qJ9UV/VOvVesuqseqqfqpXqrPqqv6qf6"
    "qwFqoBqkBqshaqgapoarEWqkGqVGqzFqrBqnxqsJaqKapCarKWqqmqamqxlqppqlZqs5aq6ap+arBWqhWqQWqyU41r8faeV/o37Of1E/9O+zH1PH1Ql1"
    "Up1Sp9UZdVadU+fhIlyGq3AdbsBNuAW34Q7chfvwUD1Sr9QT9VQ9U8/VC3ilXsPb/7gGAjd+6Svf9wM/hh/Tj+XH9uP4cf14fnw/gZ/QD/mf+In8xH4S"
    "P6mfzE/up/BT+qn81H4aX/th3/jkWz+tn86P+On9DH5GP5Of2c/iZ/Wdn83P7uf4P9f/q/41hWbQAlpBa2gDbaE9dICO0Ak6QxfoCt2gO/SE3n4fv6/f"
    "z+/vD/AH+oP8wTAUhsNIGA1jYTxMhMkwFabDTJgNc2E+LITFsBSWw0pYDWthPWyEzRAF2yDa3wG7YA/sgwNwCI5AtH8cTsJpOAvn4SJchqtwHW7ATbgF"
    "t+EO3IX78BAew1N4Di/hNbyF94DXvkACLm8QA2JBHIgHCSAEiSBJkDRIFiQPUgQpg1RB6iBNoINwYAIKbJA2SBdEgvRBhiBjkCnIHGQJsgYuyBZkD3IE"
    "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"
    "NJyF83ARLsNVuA434Kb73d1yf7jb7k93x/3l7rp77r574B66R+6xe+KeumfuuXvhXrpX7rV74966d+69YzczNCs0OzQnNDc0LzQ/tCC0MLQotDi0JLQ0"
    "tCy0PLQitDK0KrQ6tCa0NrQutD60IbQxtCm0ObQlFBXaGtoW2h6KDu0I7QztCu0O7Qkxpz5pOS2n4win5wyckTNxZs7CWdlxNs7OOTgn5+Lc/Cnn4byc"
    "j/NzAS7IhbgBN+RG3JibcFP+kptxc27BLbkVf8Wt+Wtuw99wW27H7flb7sDfcUf+njvxD9yZf+Qu/BN35Z+5G//C3bkH9+Re3Jv7cF/ux/15AA/kQTyY"
    "h/BQHsbDeQSP5FE8msfwWB7H43kCT+RJPJmn8FSextN5Bs/kWTyb5/BcnsfzeQEv5EW8mJfwUl7Gy3kFr+RVvJrX8Fpex+t5A2/kTbyZt3AUb+VtvJ2j"
    "eQfv5F28m/fwXt7H+/kAH+RDfJiP8FE+xsf5BJ/kU3yaz/BZPsfn+QJf5Et8ma/wVb7G1/lXvsG/8U3+nW/xH3yb/+Q7/Bff5Xt8nx/wQ37Ej/kJP+Vn"
    "/Jxf8Et+xa/5Db/ld/yemf8NWc4hRg=="
)
UPCASE = zlib.decompress(base64.b64decode("".join(_UPCASE_Z)))
UPCASE_CHECKSUM = 0xE619D30D
UPCASE_MAP = _expand(UPCASE)
