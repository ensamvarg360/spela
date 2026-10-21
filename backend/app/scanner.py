"""Library scanner. Classification needs content evidence (file signature or
sce_sys/param.json); an extension alone yields `unknown`. Sidecar JSON is
descriptive metadata only and never decides format or compatibility."""
import fnmatch
import json
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog, Game, LibraryRoot, now
from .pkg import CONTENT_ID_RE, PLATFORM_PREFIX, parse_pkg

TITLE_ID_RE = re.compile(r"(?<![A-Za-z0-9])(PPSA|CUSA)(\d{5})(?!\d)")  # strict: uppercase, no separator
PARTIAL_SUFFIXES = (".partial", ".part", ".tmp")
PARTIAL_MARKER = ".partial"  # marker file inside a folder dump
SIDECAR_NAMES = ("game.json", "metadata.json")
# OS-managed folders on drive roots; unreadable or irrelevant, never scanned or offered in the browser.
SYSTEM_DIRS = ("$RECYCLE.BIN", "System Volume Information", "lost+found", ".Trash-*", "$Extend")


def is_system_dir(name: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in SYSTEM_DIRS)


# Release extras that are neither a game nor DLC: firmware backports of the main package and bonus apps. Lowercase.
NOT_A_GAME = ("backport*", "*artbook*", "*soundtrack*")
# DLC is told apart by content (package content type, dlc_emu.ini). Only an archive can't be looked into, so one
# under a folder named like this counts as DLC. Lowercase.
DLC_NAMES = ("dlc", "dlcs", "*dlc.unlocker*", "*dlc.only*", "*dlc.ps5*")
UNLOCKER_INI = "dlc_emu.ini"  # DLC unlocker: copied over the game's folder, its fakelib/ answers the game's DLC checks


def is_not_a_game(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), p) for p in NOT_A_GAME)


def is_dlc_name(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name.lower(), p) for p in DLC_NAMES)

# name -> (offset, magic). Add entries here when a new format's header is known.
# PFS/PFSC/package magics verified against LibProsperoPKG; UFS2 is the standard FreeBSD superblock magic.
SIGNATURES = {
    "exfat": (3, b"EXFAT   "),
    "pfs": (8, (20130315).to_bytes(8, "little")),  # superblock: version u64 @0, magic u64 @8
    "pfsc": (0, b"PFSC\0\0\0\0"),  # compressed PFS: magic, then u32 0
    "ufs2": (0x1055C, (0x19540119).to_bytes(4, "little")),  # superblock at 64 KiB, fs_magic at +0x55C
    "ps4_pkg": (0, b"\x7fCNT"),
    "ps5_pkg": (0, b"\x7fFIH"),
    "ps5_mpkg": (0, b"PS5MPKG1"),  # multi-part PS5 package slice
    "zip": (0, b"PK\x03\x04"),
    "7z": (0, b"7z\xbc\xaf\x27\x1c"),
    "rar": (0, b"Rar!\x1a\x07"),
}
HEADER_BYTES = max(off + len(magic) for off, magic in SIGNATURES.values())
ARCHIVE_SIGS = {"zip", "7z", "rar"}
PKG_SIGS = {"ps4_pkg", "ps5_pkg", "ps5_mpkg"}

# extension -> (format, subtype, signatures that confirm it)
EXTENSIONS = {
    ".exfat": ("image", "exfat", {"exfat"}),
    ".ffpkg": ("image", "ffpkg", {"ufs2"}),
    ".ffpfs": ("image", "ffpfs", {"pfs", "exfat"}),
    ".ffpfsc": ("image", "ffpfsc", {"pfsc", "exfat"}),  # MkPFS wraps .ffpfsc in exFAT by default
    ".pkg": ("pkg", None, PKG_SIGS),
    ".zip": ("archive", None, ARCHIVE_SIGS),
    ".7z": ("archive", None, ARCHIVE_SIGS),
    ".rar": ("archive", None, ARCHIVE_SIGS),
    ".001": ("archive", None, ARCHIVE_SIGS),  # first volume of a split archive
}
LATER_VOLUME_RE = re.compile(r"(\.part(?!0*1\.)\d+\.(rar|pkg)|\.(?!0*1$)\d{3}|\.z\d\d)$", re.I)
# Strips volume markers so every part of a split set shares one key (a.part2.rar, a.7z.003, a.z01 -> a).
VOLUME_KEY_RE = re.compile(r"((\.part\d+)?\.(rar|pkg|zip|7z|z\d\d)(\.\d{3})?|\.\d{3})$", re.I)


@dataclass
class Item:
    source_path: str
    detected_format: str
    image_subtype: str | None = None
    title_id: str | None = None
    content_id: str | None = None
    metadata_source: str = "none"  # ps5_cnt|ps5_param_json|ps4_cnt|ps4_param_sfo|unlocker_ini|sidecar|filename|manual|none
    metadata_confidence: str = "unknown"  # high|medium|low|unknown
    provisional_title: str = ""
    version: str | None = None
    region: str | None = None
    size_bytes: int = 0
    fingerprint: str = ""
    cover_path: str | None = None
    sidecar: dict = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    partial: bool = False
    kind: str = "game"  # game|dlc


def _read_header(path: Path, n=HEADER_BYTES) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def _match_signature(header: bytes) -> str | None:
    for name, (off, magic) in SIGNATURES.items():
        if header[off:off + len(magic)] == magic:
            return name
    return None


def _title_from_name(name: str) -> tuple[str | None, str]:
    m = TITLE_ID_RE.search(name)
    title = TITLE_ID_RE.sub("", name)
    title = re.sub(r"[\[(][^\])]*[\])]|[_.]+", " ", title)
    return (m[0] if m else None), " ".join(title.split()) or name


def _filename_fallback(item: Item, stem: str, why: str):
    """Last resort: strict title-ID match in the name, flagged low confidence."""
    tid = _title_from_name(stem)[0]
    prefix = PLATFORM_PREFIX.get(item.detected_format)
    if tid and prefix and not tid.startswith(prefix):
        item.warnings.append(f"Filename title ID {tid} doesn't match {item.detected_format}; ignored.")
        return
    if tid:
        item.title_id, item.metadata_source, item.metadata_confidence = tid, "filename", "low"
        item.warnings.append(f"{why} Title ID derived from filename.")


def _load_sidecar(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


COVER_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")  # covers are served from the app's origin: never HTML or SVG


def _safe_child(base: Path, rel) -> str | None:
    """Resolve a sidecar-supplied cover path, refusing anything that escapes base or isn't a plain image."""
    if not isinstance(rel, str) or not rel:
        return None
    p = (base / rel).resolve()
    ok = p.is_relative_to(base.resolve()) and p.suffix.lower() in COVER_SUFFIXES and p.is_file()
    return str(p) if ok else None


def detect_folder(path: Path) -> Item | None:
    param = path / "sce_sys" / "param.json"
    if not param.is_file():
        return None
    item = Item(str(path), "folder_dump", evidence={"signature": "sce_sys/param.json"})
    try:
        p = json.loads(param.read_text(encoding="utf-8"))
        item.title_id = p.get("titleId")
        item.version = p.get("contentVersion")
        loc = p.get("localizedParameters", {})
        item.provisional_title = loc.get(loc.get("defaultLanguage", ""), {}).get("titleName", "")
        item.content_id = p.get("contentId")
        item.evidence["param_json"] = {"titleId": item.title_id, "contentVersion": item.version}
    except (OSError, ValueError, AttributeError) as e:
        item.warnings.append(f"param.json unreadable: {e}")
    if item.title_id and TITLE_ID_RE.fullmatch(item.title_id):
        item.metadata_source, item.metadata_confidence = "ps5_param_json", "high"
    else:
        item.title_id = None
        _filename_fallback(item, path.name, "param.json has no valid title ID.")
    _tree_stats(item, path)
    icon = path / "sce_sys" / "icon0.png"
    item.cover_path = str(icon) if icon.is_file() else None
    for name in SIDECAR_NAMES:
        if (path / name).is_file():
            item.sidecar = _load_sidecar(path / name)
            break
    return item


def _tree_stats(item: Item, path: Path):
    total = count = latest = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            st = os.stat(os.path.join(dirpath, f))
            total, count, latest = total + st.st_size, count + 1, max(latest, st.st_mtime_ns)
    item.size_bytes = total
    item.fingerprint = f"dir:{total}:{count}:{latest}"
    item.partial = (path / PARTIAL_MARKER).exists()


def detect_unlocker(path: Path) -> Item | None:
    """A DLC unlocker folder: dlc_emu.ini lists the DLC's content IDs, which name the game it belongs to."""
    ini = path / UNLOCKER_INI
    if not ini.is_file():
        return None
    item = Item(str(path), "unlocker", kind="dlc", evidence={"signature": UNLOCKER_INI},
                provisional_title=_title_from_name(path.name)[1])
    try:
        ids = re.findall(r"^\s*content_id\s*=\s*(\S+)", ini.read_text(encoding="utf-8", errors="replace"), re.M)
    except OSError as e:
        ids = []
        item.warnings.append(f"{UNLOCKER_INI} unreadable: {e}")
    tids = {m[1] for cid in ids if (m := CONTENT_ID_RE.match(cid))}
    item.evidence["content_ids"] = ids[:50]
    if len(tids) == 1:
        item.title_id, item.metadata_source, item.metadata_confidence = tids.pop(), "unlocker_ini", "high"
    else:
        if tids:
            item.warnings.append(f"{UNLOCKER_INI} names several games: {', '.join(sorted(tids))}.")
        _filename_fallback(item, path.name, f"{UNLOCKER_INI} names no single game.")
    _tree_stats(item, path)
    return item


def detect_file(path: Path) -> Item | None:
    name = path.name
    partial = name.lower().endswith(PARTIAL_SUFFIXES)
    if partial:
        name = name[: name.rfind(".")]
    if LATER_VOLUME_RE.search(name):
        return None  # later volume of a split archive; the first volume represents it
    ext = Path(name).suffix.lower()
    if ext not in EXTENSIONS:
        return None
    fmt, subtype, accepted = EXTENSIONS[ext]
    st = path.stat()
    header = _read_header(path)
    sig = _match_signature(header)
    item = Item(str(path), fmt, subtype, size_bytes=st.st_size,
                fingerprint=f"file:{st.st_size}:{st.st_mtime_ns}", partial=partial,
                evidence={"extension": ext, "signature": sig})
    if sig not in accepted:
        item.detected_format, item.image_subtype = "unknown", None
        item.evidence["reason"] = f"extension {ext} but content signature is {sig or 'unrecognized'}"
        item.evidence["header_hex"] = header[:16].hex()
    pkg_title = None
    if sig in accepted and fmt == "pkg":
        meta = parse_pkg(str(path))
        item.detected_format, item.kind = meta.platform, "dlc" if meta.dlc else "game"
        item.title_id, item.content_id, item.version, item.region = meta.title_id, meta.content_id, meta.version, meta.region
        item.metadata_source, item.metadata_confidence = meta.source, meta.confidence
        item.warnings += meta.warnings
        item.evidence["pkg"] = {"category": meta.category, "min_firmware": meta.min_firmware, "multipart": meta.multipart,
                                "dlc": meta.dlc}
        pkg_title = meta.title if meta.title_id else None
        folder_tid = _title_from_name(path.parent.name)[0]
        if meta.title_id and folder_tid and folder_tid != meta.title_id:
            item.warnings.append(f"Package is {meta.title_id} but its folder says {folder_tid}; the release may be mislabeled.")
    stem = Path(name).stem
    item.provisional_title = pkg_title or _title_from_name(stem)[1]
    if not item.title_id and item.detected_format != "unknown":
        why = {"ps5_pkg": "PS5 CNT metadata could not be parsed.", "ps4_pkg": "PS4 package metadata could not be parsed."}
        _filename_fallback(item, stem, why.get(item.detected_format, "No embedded metadata."))
    for side in (path.with_name(name + ".json"), path.with_name(stem + ".json")):
        if side.is_file():
            item.sidecar = _load_sidecar(side)
            break
    return item


FOLDER_DETECTORS = [detect_folder, detect_unlocker]  # in order: a game folder with an unlocker copied in is a game
FILE_DETECTORS = [detect_file]


def _excluded(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(os.path.basename(rel), p) for p in patterns)


def scan_root(root: str, excludes: list[str] | None = None, kind: str = "game") -> tuple[list[Item], list[str]]:
    """Return (items, failed_dirs). Raises OSError if the root itself is unreadable.
    `kind` is the root's: in a DLC root every archive counts as DLC, elsewhere only one under a DLC-named folder."""
    excludes = excludes or []
    os.listdir(root)  # surface an unreachable mount before walking
    items, failed, later_volumes = [], [], {}
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: failed.append(e.filename)):
        keep = []
        for d in sorted(dirnames):
            full = Path(dirpath, d)
            if is_system_dir(d) or is_not_a_game(d) or _excluded(os.path.relpath(full, root), excludes):
                continue
            try:
                found = next((i for det in FOLDER_DETECTORS if (i := det(full))), None)
            except OSError:
                failed.append(str(full))
                continue
            if found:
                items.append(found)  # don't descend into a recognised dump
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            full = Path(dirpath, f)
            if f in SIDECAR_NAMES or is_not_a_game(f) or _excluded(os.path.relpath(full, root), excludes):
                continue
            if LATER_VOLUME_RE.search(f):
                later_volumes.setdefault(_volume_key(full), str(full))
                continue
            try:
                found = next((i for det in FILE_DETECTORS if (i := det(full))), None)
            except OSError:
                failed.append(str(full))
                continue
            if found:
                if found.detected_format == "archive" and (kind == "dlc" or any(
                        is_dlc_name(p) for p in Path(os.path.relpath(full, root)).parts)):
                    found.kind = "dlc"
                items.append(found)
    first_volumes = {_volume_key(Path(it.source_path)) for it in items}
    for key, path in later_volumes.items():
        if key not in first_volumes:
            items.append(Item(path, "unknown", evidence={"reason": "split set whose first part is missing"},
                              warnings=["Split set: first part is missing."]))
    for it in items:
        _apply_sidecar(it)
    return items, failed


def _volume_key(path: Path) -> str:
    return str(path.with_name(VOLUME_KEY_RE.sub("", path.name).lower()))


def _apply_sidecar(it: Item):
    s = it.sidecar
    if not s:
        return
    base = Path(it.source_path) if it.detected_format in ("folder_dump", "unlocker") else Path(it.source_path).parent
    if not it.title_id and isinstance(s.get("title_id"), str) and TITLE_ID_RE.fullmatch(s["title_id"]):
        it.title_id, it.metadata_source, it.metadata_confidence = s["title_id"], "sidecar", "low"
    it.version = it.version or s.get("version")
    if s.get("title"):
        it.provisional_title = s["title"]
    it.cover_path = _safe_child(base, s.get("cover")) or it.cover_path
    if s.get("format") and s["format"] != it.detected_format:
        it.warnings.append(f"sidecar claims format {s['format']!r}; ignored")
    it.evidence["sidecar_keys"] = sorted(s)


# ---------------------------------------------------------------- database sync

SIDECAR_FIELDS = ("region", "description", "owned_copy_type", "genre", "provenance_notes")

scan_lock = threading.Lock()
scan_state: dict = {"running": False, "report": None, "finished_at": None}


def _is_under(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip("/\\") + os.sep) for r in roots)


def evaluate(game: Game, duplicate_ids: set[str], game_ids: set[str] = frozenset()) -> tuple[str, str]:
    """Library-side compatibility. Console-side checks (firmware, space, helper) run at preflight.
    `game_ids`: title IDs of the games in the library (archives included), so DLC can tell its game is there."""
    if game.missing:
        return "incompatible", "Source path is missing or unreadable."
    if game.format == "unknown":
        return "incompatible", f"Format not recognized ({game.detection_evidence.get('reason', 'no signature')})."
    reasons = []
    if game.detection_evidence.get("partial"):
        reasons.append("Source looks like an incomplete transfer.")
    if game.format == "archive":
        reasons.append("Archive: it is extracted on this PC before it is installed.")
    if game.kind == "dlc" and game.title_id and game.title_id not in game_ids:
        reasons.append(f"Its game ({game.title_id}) isn't in your library.")
    prefix = PLATFORM_PREFIX.get(game.format)
    if game.detection_evidence.get("pkg", {}).get("multipart"):
        reasons.append("Multi-part package: verify all parts are present.")
    if not game.title_id:
        reasons.append("Title ID unknown.")
    elif prefix and not game.title_id.startswith(prefix):
        reasons.append(f"Title ID {game.title_id} doesn't match {game.format}.")
    elif game.metadata_confidence in ("low", "unknown"):
        reasons.append(f"Title ID comes from the {game.metadata_source}, not package metadata; verify it.")
    elif game.title_id in duplicate_ids:
        reasons.append("Another copy in your library has the same title ID; a console holds only one.")
    return ("needs_attention", " ".join(reasons)) if reasons else ("ready", "")


def refresh_statuses(db: Session):
    games = db.scalars(select(Game)).all()
    seen, dups = set(), set()
    game_ids = {g.title_id for g in games if g.kind == "game" and g.title_id and not g.missing}
    for g in games:
        # An archive next to its extracted copy is a backup pair, not a duplicate. DLC shares its game's title ID.
        if g.title_id and not g.missing and g.format != "archive" and g.kind == "game":
            (dups if g.title_id in seen else seen).add(g.title_id)
    for g in games:
        g.compatibility_status, g.compatibility_reason = evaluate(g, dups, game_ids)


def _upsert(db: Session, it: Item, existing: Game | None) -> str | None:
    evidence = {**it.evidence, "partial": it.partial}
    if existing and existing.source_fingerprint == it.fingerprint and not existing.missing:
        return None
    g = existing or Game(source_path=it.source_path, title=it.provisional_title or Path(it.source_path).name)
    g.format, g.image_subtype, g.kind = it.detected_format, it.image_subtype, it.kind
    g.size_bytes, g.source_fingerprint = it.size_bytes, it.fingerprint
    g.compressed_size_bytes = it.size_bytes if it.detected_format == "archive" else None
    g.detection_evidence, g.missing = evidence, False
    g.content_id, g.parser_warnings = it.content_id, it.warnings
    if g.metadata_source != "manual":  # never overwrite a title ID the user confirmed
        g.title_id, g.metadata_source, g.metadata_confidence = it.title_id, it.metadata_source, it.metadata_confidence
    g.version = it.version or g.version
    g.region = g.region or it.region
    g.cover_path = g.cover_path or it.cover_path
    if not existing:
        for f in SIDECAR_FIELDS:
            if isinstance(it.sidecar.get(f), str):
                setattr(g, f, it.sidecar[f])
        db.add(g)
        return "added"
    return "updated"


def run_scan(db: Session) -> dict:
    report = {"added": [], "updated": [], "missing": [], "unknown": [], "invalid": [], "errors": []}
    all_roots = db.scalars(select(LibraryRoot)).all()
    roots = [r for r in all_roots if r.enabled]
    by_path = {}
    for g in db.scalars(select(Game)):
        if _is_under(g.source_path, [r.path for r in all_roots]):
            by_path[g.source_path] = g
        else:
            db.delete(g)  # its library folder was removed
    seen, scanned_roots, failed_dirs = set(), [], []
    for root in roots:
        try:
            items, failed = scan_root(root.path, root.exclude_patterns, root.kind)
        except OSError as e:
            report["errors"].append(f"{root.path}: {e}")
            continue
        scanned_roots.append(root.path)
        failed_dirs += failed
        report["errors"] += [f"unreadable: {p}" for p in failed]
        for it in items:
            if it.source_path in seen:
                continue  # overlapping roots
            seen.add(it.source_path)
            change = _upsert(db, it, by_path.get(it.source_path))
            if change:
                report[change].append(it.source_path)
            if it.detected_format == "unknown":
                report["unknown"].append(it.source_path)
            if it.partial or it.warnings:
                report["invalid"].append(it.source_path)
    # Only mark missing what lives under a root we actually walked, outside any dir that errored.
    for path, g in by_path.items():
        if path not in seen and any(is_not_a_game(p) for p in Path(path).parts):
            db.delete(g)  # catalogued before the extras filter existed; holds no user data worth keeping
        elif path not in seen and not g.missing and _is_under(path, scanned_roots) and not _is_under(path, failed_dirs):
            g.missing = True
            report["missing"].append(path)
    db.flush()
    refresh_statuses(db)
    db.add(AuditLog(action="scan", result="failed" if report["errors"] else "success",
                    details={k: len(v) for k, v in report.items()} | {"errors": report["errors"][:50]}))
    db.commit()
    return report


def run_scan_locked(session_factory) -> dict | None:
    """Run a scan unless one is already running. Returns None if skipped."""
    if not scan_lock.acquire(blocking=False):
        return None
    scan_state["running"] = True
    try:
        with session_factory() as db:
            scan_state["report"] = run_scan(db)
        return scan_state["report"]
    finally:
        scan_state["running"], scan_state["finished_at"] = False, now().isoformat()
        scan_lock.release()
