"""Read-only package metadata parser. Reads only headers and small entries; never the payload.

Layouts follow LibProsperoPKG (src/LibProsperoPkg/PKG/ProsperoPkgReader.cs):
- PS4 package: CNT container at offset 0, magic \\x7fCNT.
- PS5 package: FIH header, magic \\x7fFIH; embedded CNT offset is a little-endian u64 at 0x58.
- CNT header is big-endian: entry count u32 @0x10, entry table offset u32 @0x18, content ID @0x40 (36 chars).
- Entry record (0x20 bytes, BE): id, name offset, flags1, flags2, data offset, data size.
  Offsets are relative to the CNT start. flags1 & 0x80000000 = encrypted.
- Entry 0x1000 = param.sfo (PS4), 0x2000 = param.json (PS5).
"""
import json
import re
import struct
from dataclasses import dataclass, field

FIH_MAGIC, CNT_MAGIC, MPKG_MAGIC = b"\x7fFIH", b"\x7fCNT", b"PS5MPKG1"
ENTRY_PARAM_SFO, ENTRY_PARAM_JSON = 0x1000, 0x2000
MAX_PARAM_SIZE = 1 << 20
PLATFORM_PREFIX = {"ps5_pkg": "PPSA", "ps4_pkg": "CUSA"}
CONTENT_ID_RE = re.compile(r"^[A-Z]{2}\d{4}-([A-Z]{4}\d{5})_\d{2}-[A-Z0-9]{16}$")
REGIONS = {"U": "US", "E": "EU", "J": "JP", "H": "Asia", "K": "KR", "I": "INT"}


@dataclass
class PkgMetadata:
    platform: str = "unknown"  # ps4_pkg|ps5_pkg|unknown
    title_id: str | None = None
    content_id: str | None = None
    title: str | None = None
    version: str | None = None
    category: str | None = None
    region: str | None = None
    min_firmware: str | None = None
    multipart: bool = False
    source: str = "none"  # ps5_cnt|ps5_param_json|ps4_cnt|ps4_param_sfo|none
    confidence: str = "unknown"  # high|medium|low|unknown
    warnings: list[str] = field(default_factory=list)


def _cnt(f, base: int) -> tuple[str, dict[int, tuple[int, int, int]]]:
    f.seek(base)
    hdr = f.read(0x80)
    if hdr[:4] != CNT_MAGIC:
        raise ValueError(f"no CNT magic at 0x{base:x}")
    count, table = struct.unpack_from(">I", hdr, 0x10)[0], struct.unpack_from(">I", hdr, 0x18)[0]
    if count > 0x10000:
        raise ValueError("CNT entry count out of range")
    content_id = hdr[0x40:0x64].split(b"\0")[0].decode("ascii", "replace")
    f.seek(base + table)
    raw = f.read(count * 0x20)
    if len(raw) < count * 0x20:
        raise ValueError("CNT entry table truncated")
    entries = {}
    for i in range(count):
        eid, _, flags1, _, off, size = struct.unpack_from(">6I", raw, i * 0x20)
        entries[eid] = (flags1, base + off, size)
    return content_id, entries


def _entry(f, entries, eid: int, name: str, warnings: list[str]) -> bytes | None:
    if eid not in entries:
        warnings.append(f"{name} entry not found in CNT.")
        return None
    flags1, off, size = entries[eid]
    if flags1 & 0x80000000:
        warnings.append(f"{name} is encrypted; skipped.")
        return None
    if size > MAX_PARAM_SIZE:
        warnings.append(f"{name} entry is implausibly large; skipped.")
        return None
    f.seek(off)
    return f.read(size)


def parse_sfo(data: bytes) -> dict[str, str | int]:
    if data[:4] != b"\0PSF":
        raise ValueError("bad param.sfo magic")
    _, key_start, data_start, n = struct.unpack_from("<4I", data, 4)
    out = {}
    for i in range(n):
        key_off, fmt, length, _, data_off = struct.unpack_from("<HHIII", data, 20 + i * 16)
        key = data[key_start + key_off:data.index(b"\0", key_start + key_off)].decode("ascii")
        val = data[data_start + data_off:data_start + data_off + length]
        out[key] = struct.unpack("<I", val[:4])[0] if fmt == 0x0404 else val.split(b"\0")[0].decode("utf-8", "replace")
    return out


def _title_id_ok(meta: PkgMetadata, tid) -> bool:
    return isinstance(tid, str) and re.fullmatch(rf"{PLATFORM_PREFIX[meta.platform]}\d{{5}}", tid) is not None


def parse_pkg(path: str) -> PkgMetadata:
    meta = PkgMetadata()
    with open(path, "rb") as f:
        head = f.read(0x400)
        if head[:4] == FIH_MAGIC:
            meta.platform, base = "ps5_pkg", struct.unpack_from("<Q", head, 0x58)[0]
        elif head[:4] == CNT_MAGIC:
            meta.platform, base = "ps4_pkg", 0
        elif head[:8] == MPKG_MAGIC:
            # ponytail: multi-part slice header layout unconfirmed; take the title ID from its text fields.
            meta.platform, meta.multipart, meta.source, meta.confidence = "ps5_pkg", True, "ps5_cnt", "medium"
            m = re.search(rb"PPSA\d{5}", head)
            meta.title_id = m[0].decode() if m else None
            meta.warnings.append("Multi-part package slice; verify all parts are present.")
            return meta
        else:
            meta.warnings.append("Unrecognized package header.")
            return meta

        try:
            cid, entries = _cnt(f, base)
        except (ValueError, struct.error, OSError) as e:
            meta.warnings.append(f"{'PS5' if meta.platform == 'ps5_pkg' else 'PS4'} CNT metadata could not be parsed: {e}.")
            return meta

        meta.content_id = cid
        m = CONTENT_ID_RE.match(cid)
        if m and _title_id_ok(meta, m[1]):
            meta.title_id, meta.source, meta.confidence = m[1], meta.platform[:3] + "_cnt", "high"
            meta.region = REGIONS.get(cid[0])
        else:
            meta.warnings.append(f"Content ID {cid!r} doesn't contain a valid {PLATFORM_PREFIX[meta.platform]} title ID.")

        try:
            if meta.platform == "ps5_pkg":
                _read_param_json(meta, _entry(f, entries, ENTRY_PARAM_JSON, "param.json", meta.warnings))
            else:
                _read_param_sfo(meta, _entry(f, entries, ENTRY_PARAM_SFO, "param.sfo", meta.warnings))
        except (ValueError, KeyError, struct.error, AttributeError) as e:
            meta.warnings.append(f"Package parameters unreadable: {e}.")
    return meta


def _read_param_json(meta: PkgMetadata, data: bytes | None):
    if data is None:
        return
    p = json.loads(data.rstrip(b"\0"))
    _merge_title_id(meta, p.get("titleId"), "ps5_param_json")
    meta.content_id = meta.content_id or p.get("contentId")
    meta.version = p.get("contentVersion") or p.get("masterVersion")
    meta.category = str(p["applicationCategoryType"]) if "applicationCategoryType" in p else None
    meta.min_firmware = p.get("requiredSystemSoftwareVersion")
    loc = p.get("localizedParameters", {})
    meta.title = loc.get(loc.get("defaultLanguage", ""), {}).get("titleName")


def _read_param_sfo(meta: PkgMetadata, data: bytes | None):
    if data is None:
        return
    p = parse_sfo(data)
    _merge_title_id(meta, p.get("TITLE_ID"), "ps4_param_sfo")
    meta.title, meta.version, meta.category = p.get("TITLE"), p.get("APP_VER") or p.get("VERSION"), p.get("CATEGORY")
    if isinstance(fw := p.get("SYSTEM_VER"), int):
        meta.min_firmware = f"0x{fw:08x}"


def _merge_title_id(meta: PkgMetadata, tid, source: str):
    if not _title_id_ok(meta, tid):
        if tid:
            meta.warnings.append(f"{source} title ID {tid!r} doesn't match the {meta.platform} pattern; ignored.")
        return
    if meta.title_id and meta.title_id != tid:
        meta.warnings.append(f"Title ID differs between CNT ({meta.title_id}) and {source} ({tid}).")
        meta.confidence = "medium"
        return
    meta.title_id, meta.source, meta.confidence = tid, source, meta.confidence if meta.title_id else "high"
