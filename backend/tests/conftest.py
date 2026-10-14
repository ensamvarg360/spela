import json
import os
import struct
import tempfile

_data = tempfile.mkdtemp()
os.environ.update(SECRET_KEY="test-secret", DATA_DIR=_data,
                  DATABASE_URL=f"sqlite:///{_data}/test.db", SCAN_INTERVAL_MINUTES="0")

import pytest  # noqa: E402

EXFAT = b"\xebv\x90EXFAT   " + b"\0" * 200
UFS2 = b"\0" * 0x1055C + (0x19540119).to_bytes(4, "little")


def write(path, data: bytes = b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def cnt(content_id: str, entries: dict[int, bytes], encrypted=()) -> bytes:
    """Minimal CNT container: header, 0x20-byte BE entry records, then entry data (offsets CNT-relative)."""
    hdr = bytearray(0x100)
    hdr[:4] = b"\x7fCNT"
    struct.pack_into(">I", hdr, 0x10, len(entries))
    struct.pack_into(">I", hdr, 0x18, 0x100)
    hdr[0x40:0x40 + len(content_id)] = content_id.encode()
    table, body, data_off = b"", b"", 0x100 + len(entries) * 0x20
    for eid, d in entries.items():
        flags1 = 0x80000000 if eid in encrypted else 0
        table += struct.pack(">6I8x", eid, 0, flags1, 0, data_off + len(body), len(d))
        body += d
    return bytes(hdr) + table + body


def ps5_pkg(cnt_bytes: bytes, cnt_offset=0x1000) -> bytes:
    fih = bytearray(cnt_offset)
    fih[:4], fih[5] = b"\x7fFIH", 0x80
    struct.pack_into("<Q", fih, 0x58, cnt_offset)
    return bytes(fih) + cnt_bytes


def sfo(values: dict[str, str]) -> bytes:
    index, keys, data = b"", b"", b""
    for k, v in values.items():
        vb = v.encode() + b"\0"
        index += struct.pack("<HHIII", len(keys), 0x0204, len(vb), len(vb), len(data))
        keys += k.encode() + b"\0"
        data += vb
    key_start = 20 + len(index)
    return b"\0PSF" + struct.pack("<4I", 0x101, key_start, key_start + len(keys), len(values)) + index + keys + data


PARAM_JSON = json.dumps({
    "titleId": "PPSA02222", "contentId": "EP0001-PPSA02222_00-EXAMPLEGAME00000", "contentVersion": "01.020.000",
    "applicationCategoryType": 0, "requiredSystemSoftwareVersion": "0x0400000000000000",
    "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Native Game"}},
}).encode()


@pytest.fixture
def library(tmp_path):
    """A sample library covering every format category."""
    dump = tmp_path / "Example Game (PPSA01234)"
    write(dump / "sce_sys" / "param.json", json.dumps({
        "titleId": "PPSA01234", "contentVersion": "01.020.000",
        "localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": "Example Game"}},
    }).encode())
    write(dump / "sce_sys" / "icon0.png", b"png")
    write(dump / "eboot.bin", b"x" * 100)
    write(dump / "game.json", json.dumps({"region": "EU", "format": "ps5_pkg", "cover": "../../etc/passwd"}).encode())
    write(tmp_path / "Other_PPSA05555.exfat", EXFAT)
    write(tmp_path / "fake.ffpfs", b"not an image at all" * 10)  # extension lies
    write(tmp_path / "Old CUSA00001.pkg", cnt("UP0000-CUSA00001_00-GAME000000000000", {
        0x1000: sfo({"TITLE_ID": "CUSA00001", "TITLE": "Old Game", "APP_VER": "01.05", "CATEGORY": "gd"})}))
    write(tmp_path / "renamed.pkg", ps5_pkg(cnt("EP0001-PPSA02222_00-EXAMPLEGAME00000", {0x2000: PARAM_JSON})))
    write(tmp_path / "Broken PPSA03333.pkg", ps5_pkg(b"garbage" * 40))
    write(tmp_path / "Archived CUSA00002.7z.001", b"7z\xbc\xaf\x27\x1c" + b"\0" * 50)
    write(tmp_path / "Archived CUSA00002.7z.002", b"\0" * 50)
    write(tmp_path / "Lost.part2.rar", b"Rar!\x1a\x07" + b"\0" * 50)  # first part missing
    write(tmp_path / "Half PPSA07777.exfat.part", EXFAT)
    write(tmp_path / "Copy (PPSA01234).ffpkg", UFS2)
    write(tmp_path / "Comp PPSA04444.ffpfsc", b"PFSC\0\0\0\0" + b"\0" * 100)
    write(tmp_path / "skip" / "Excluded PPSA09999.exfat", EXFAT)
    # Release extras that must never be catalogued.
    write(tmp_path / "Rel.PS5-PPSA02222[FPKG]" / "backport (4.xx Only)" / "x-4xx.pkg", ps5_pkg(b"junk"))
    write(tmp_path / "Rel.PS5-PPSA02222[FPKG]" / "DLCs" / "addon.pkg", ps5_pkg(b"junk"))
    write(tmp_path / "Game.DLC.Unlocker.PS5-GRP" / "unlocker.rar", b"Rar!\x1a\x07")
    write(tmp_path / "Mini_Artbook_PPSA05999-app0" / "sce_sys" / "param.json", b"{}")
    # Mislabeled release: folder says PPSA08888, the package inside is PPSA02222.
    write(tmp_path / "Wrong.PS5-PPSA08888[FPKG]" / "main.pkg", ps5_pkg(cnt("EP0001-PPSA02222_00-EXAMPLEGAME00000", {0x2000: PARAM_JSON})))
    write(tmp_path / "notes.txt", b"hello")
    return tmp_path
