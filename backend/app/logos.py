"""Title logos on the PS5 home screen. A backup's home-screen page shows its name in plain text: the console gets the
logo from PlayStation Network, which a jailbroken console doesn't reach. This PC looks it up in Sony's catalog by the
game's concept ID, read from the console's own database, and the helper puts it on the console.
Also the library's PlayStation Store lookups by title ID: posters and store facts for the tiles."""
import asyncio
import io
import json
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from . import config, inventory
from .consoles import AdapterError
from .database import SessionLocal
from .models import AuditLog, Console

CATALOG = "https://m.np.playstation.com/api/catalog/v2/concepts/{}?country=US&language=en-US&age=99"  # no login needed
# The concept (the game across editions and regions) behind a title ID; a list, empty when the store has none
TITLE = "https://m.np.playstation.com/api/catalog/v2/titles/{}_00/concepts?country={}&language={}&age=99"
PRODUCT = "https://m.np.playstation.com/api/catalog/v2/products/{}?country={}&language={}&age=99"  # one edition
STORES = (("US", "en-US"), ("GB", "en-GB"), ("JP", "ja-JP"))  # English first, so a Japanese release gets English text
TITLE_ID = re.compile(r"(PPSA|CUSA)\d{5}")
CONTENT_ID = re.compile(r"[A-Z]{2}\d{4}-[A-Z]{4}\d{5}_\d{2}-[A-Z0-9]{16}")
IMAGES = "https://image.api.playstation.com/"
PNG = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 16 << 20  # the helper's limit
# per title ID: <id>.json (its store concept, {} when none), <id>.jpg poster, <id>.none when the store has no art
STORE_DIR = config.DATA_DIR / "store"
RETRY_SECONDS = 7 * 86400  # a game the store didn't have is asked about again after this


def concept_id(home_db: bytes, title_id: str) -> int | None:
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(home_db)
        row = db.execute("SELECT conceptId FROM tbl_contentinfo WHERE titleId = ?", (title_id,)).fetchone()
    except sqlite3.DatabaseError:
        raise AdapterError("home_db_unreadable", "The console's home-screen database couldn't be read.")
    finally:
        db.close()
    return row[0] if row and row[0] else None  # 0 for homebrew without a concept


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(MAX_BYTES + 1)


def _save(path: Path, data: bytes):
    STORE_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(f".{threading.get_ident()}.tmp")  # tiles load in parallel
    tmp.write_bytes(data)
    os.replace(tmp, path)


def concept(title_id: str) -> dict | None:
    """The game's PlayStation Store concept, found by its title ID and kept in STORE_DIR. None when the store has
    none or can't be reached."""
    if not TITLE_ID.fullmatch(title_id):
        return None
    path = STORE_DIR / f"{title_id}.json"
    if path.is_file() and ((data := json.loads(path.read_bytes())) or time.time() - path.stat().st_mtime < RETRY_SECONDS):
        return data or None
    data = {}
    for country, language in STORES:
        try:
            found = json.loads(_get(TITLE.format(title_id, country, language)))
        except urllib.error.HTTPError:
            continue  # not sold in that store
        except (OSError, ValueError):
            return None  # offline: ask again next time
        if isinstance(found, list) and found and isinstance(found[0], dict):
            data = found[0]
            break
    _save(path, json.dumps(data).encode())
    return data or None


def _url(images: list[dict], kind: str) -> str | None:
    url = next((i.get("url") for i in images if i.get("type") == kind), None)
    return url if isinstance(url, str) and url.startswith(IMAGES) else None  # only Sony's image server


def _open(url: str | None) -> Image.Image | None:
    if not url:
        return None
    try:
        img = Image.open(io.BytesIO(_get(url)))
        img.load()
        return img
    except (OSError, Image.DecompressionBombError):  # unreachable or not an image
        return None


def _portrait_url(title_id: str, content_id: str | None) -> str | None:
    """The game's portrait key art: its own product's (its content ID), its concept's (shared by a collection's games),
    or else one of its editions' (often a Deluxe Edition's, which says so on the art), asking each store in turn.
    Raises OSError when offline."""
    def product_art(pid: str, country: str, language: str) -> str | None:
        try:
            product = json.loads(_get(PRODUCT.format(pid, country, language)))
        except urllib.error.HTTPError:
            return None  # not sold in that store
        return _url((product.get("media") or {}).get("images") or [], "PORTRAIT_BANNER")

    for country, language in STORES:
        if content_id and CONTENT_ID.fullmatch(content_id) and (url := product_art(content_id, country, language)):
            return url
        try:
            found = json.loads(_get(TITLE.format(title_id, country, language)))
        except urllib.error.HTTPError:
            continue
        if not (isinstance(found, list) and found and isinstance(found[0], dict)):
            continue
        c = found[0]
        if url := _url((c.get("media") or {}).get("images") or [], "PORTRAIT_BANNER"):
            return url
        editions = [(c.get("defaultProduct") or {}).get("id")] + [
            i for group in c.get("categorizedProducts") or [] for i in group.get("ids") or []]
        for pid in dict.fromkeys(filter(None, editions)):
            if pid != content_id and (url := product_art(pid, country, language)):
                return url
    return None


def poster(title_id: str, content_id: str | None = None) -> Path | None:
    """The game's 2:3 poster, kept in STORE_DIR: the store's portrait key art (_portrait_url), or else its landscape
    key art cropped to 2:3 with the game's logo on top. None when the store has neither."""
    path, none = STORE_DIR / f"{title_id}.jpg", STORE_DIR / f"{title_id}.none"
    if path.is_file():
        return path
    if none.is_file() and time.time() - none.stat().st_mtime < RETRY_SECONDS:
        return None
    if not (c := concept(title_id)):
        return None
    try:
        portrait = _open(_portrait_url(title_id, content_id))
    except (OSError, ValueError, AttributeError, TypeError):
        return None  # offline or a surprising answer: ask again next time
    images = (c.get("media") or {}).get("images") or []
    if portrait:
        img = ImageOps.fit(portrait.convert("RGB"), (480, 720))
    elif (art := _open(_url(images, "GAMEHUB_COVER_ART"))) and (logo := _open(_url(images, "LOGO"))):
        # ponytail: a centre crop; the subject of some key art sits off-centre
        img = ImageOps.fit(art.convert("RGB"), (480, 720))
        shade = Image.linear_gradient("L").rotate(180).resize((480, 320)).point(lambda v: v * 3 // 4)  # dark at the top
        img.paste((0, 0, 0), (0, 0, 480, 320), shade)  # so the logo reads over busy art
        logo = logo.convert("RGBA")
        logo.thumbnail((400, 200))
        img.paste(logo, ((480 - logo.width) // 2, 48), logo)
    else:
        _save(none, b"")
        return None
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    _save(path, out.getvalue())
    return path


def title_logo(title_id: str) -> Path | None:
    """The game's logo from its store concept (white lettering on a transparent PNG), kept in STORE_DIR as
    <id>-logo.png, with <id>-logo.none when the store has none. None then, or when the store can't be reached."""
    path, none = STORE_DIR / f"{title_id}-logo.png", STORE_DIR / f"{title_id}-logo.none"
    if path.is_file():
        return path
    if none.is_file() and time.time() - none.stat().st_mtime < RETRY_SECONDS:
        return None
    if not (c := concept(title_id)):
        return None
    url = _url((c.get("media") or {}).get("images") or [], "LOGO")
    try:
        png = _get(url) if url else b""
    except OSError:
        return None  # offline: ask again next time
    if not png.startswith(PNG) or len(png) > MAX_BYTES:
        _save(none, b"")
        return None
    _save(path, png)
    return path


def store_images(title_id: str, content_id: str | None = None) -> list[dict]:
    """Every image PlayStation Store lists for the game, per store: its concept's and each edition's (its own content
    ID first). [{store, name, sources: [{label, images: [{type, url}]}]}]; a store that doesn't sell it has no sources.
    Raises OSError when offline."""
    def listed(media: dict | None) -> list[dict]:
        return [{"type": i.get("type"), "url": i["url"]} for i in (media or {}).get("images") or []
                if isinstance(i, dict) and isinstance(i.get("url"), str) and i["url"].startswith(IMAGES)]

    out = []
    for country, language in STORES:
        entry = {"store": country, "name": None, "sources": []}
        out.append(entry)
        try:
            found = json.loads(_get(TITLE.format(title_id, country, language)))
        except urllib.error.HTTPError:
            continue  # not sold in that store
        if not (isinstance(found, list) and found and isinstance(found[0], dict)):
            continue
        c = found[0]
        entry["name"] = c.get("name")
        entry["sources"].append({"label": "Concept (shared by all editions)", "images": listed(c.get("media"))})
        editions = [content_id, (c.get("defaultProduct") or {}).get("id")] + [
            i for group in c.get("categorizedProducts") or [] for i in group.get("ids") or []]
        for pid in dict.fromkeys(i for i in editions if i and CONTENT_ID.fullmatch(i)):
            try:
                product = json.loads(_get(PRODUCT.format(pid, country, language)))
            except urllib.error.HTTPError:
                continue
            entry["sources"].append({"label": f"{product.get('name') or 'Edition'} ({pid})" + (" - this game" if pid == content_id else ""),
                                     "images": listed(product.get("media"))})
    return out


def cached_concept(title_id: str) -> dict | None:
    """concept() as far as STORE_DIR already knows it, without asking the store."""
    path = STORE_DIR / f"{title_id}.json"
    return json.loads(path.read_bytes()) or None if TITLE_ID.fullmatch(title_id) and path.is_file() else None


def store_info(title_id: str, cached: bool = False) -> dict | None:
    """What the store says about the game, for its tile: publisher, release date, genres, star rating, age rating.
    `cached` only reads what STORE_DIR already holds."""
    if not (c := (cached_concept if cached else concept)(title_id)):
        return None
    stars = c.get("starRating") or {}
    return {
        "publisher": c.get("publisherName") or None,
        "release_date": ((c.get("releaseDate") or {}).get("date") or "")[:10] or None,
        "genres": [g["value"] for g in c.get("localizedGenres") or []],
        "rating": round(float(stars["score"]), 1) if stars.get("score") else None,
        "rating_count": int(stars.get("total") or 0),
        "age_rating": (c.get("contentRating") or {}).get("description"),
    }


def fetch_logo(concept: int) -> bytes:
    """The game's LOGO image from Sony's catalog: white lettering on a transparent PNG."""
    try:
        images = json.loads(_get(CATALOG.format(concept)))["media"]["images"]
        url = next(i["url"] for i in images if i.get("type") == "LOGO")
        png = _get(url) if url.startswith(IMAGES) else b""  # only Sony's image server, whatever the answer says
    except StopIteration:
        raise AdapterError("no_logo", "PlayStation Store has no logo for this game.")
    except (OSError, ValueError, KeyError, TypeError):
        raise AdapterError("store_unreachable", "Couldn't get the logo from PlayStation Store. Is this PC online?")
    if not png.startswith(PNG) or len(png) > MAX_BYTES:
        raise AdapterError("no_logo", "PlayStation Store's logo for this game isn't a usable PNG.")
    return png


def audit(db: Session, console: Console, title_id: str, ok: bool, message: str):
    db.add(AuditLog(action="home_logo", console_id=console.id, title_id=title_id, result="success" if ok else "failed",
                    details={"message": message}))


def _backup(console: Console, home_db: bytes):
    """console-backups/<console id>-app.db, the copy to put back over FTP if the home screen ever breaks."""
    backup = config.DATA_DIR / "console-backups" / f"{console.id}-app.db"
    backup.parent.mkdir(exist_ok=True)
    backup.write_bytes(home_db)  # ponytail: only the latest copy; keep several if a bad write ever goes unnoticed


async def apply(db: Session, console: Console, title_id: str) -> None:
    """Puts the game's logo on `console`'s home screen, saving the console's database first (_backup)."""
    adapter = inventory.adapter_for(db, console)
    home_db = await adapter.get_home_db()
    _backup(console, home_db)
    if not (concept := concept_id(home_db, title_id)):
        raise AdapterError("not_found", "The console's home screen has no PlayStation Store entry for this game.")
    png = await asyncio.to_thread(fetch_logo, concept)
    await adapter.set_home_logo(title_id, png)
    _save(STORE_DIR / f"concept-{concept}.png", png)  # restore() puts back this one, not an older copy


def cached_logo(concept: int) -> bytes | None:
    """fetch_logo, kept in STORE_DIR as concept-<id>.png. None when the store has no logo (asked again after
    RETRY_SECONDS) or can't be reached."""
    path, none = STORE_DIR / f"concept-{concept}.png", STORE_DIR / f"concept-{concept}.none"
    if path.is_file():
        return path.read_bytes()
    if none.is_file() and time.time() - none.stat().st_mtime < RETRY_SECONDS:
        return None
    try:
        png = fetch_logo(concept)
    except AdapterError as e:
        if e.code == "no_logo":
            _save(none, b"")
        return None
    _save(path, png)
    return png


def home_games(home_db: bytes) -> dict[str, tuple[int, bool]]:
    """{title ID: (concept ID, whether its logo entry points at the logo the helper writes)} for the games on the
    home screen that have a PlayStation Store concept."""
    db = sqlite3.connect(":memory:")
    try:
        db.deserialize(home_db)
        rows = db.execute("SELECT c.titleId, c.conceptId, m.logoImageInfo FROM tbl_contentinfo c LEFT JOIN "
                          "tbl_conceptmetadata m ON m.localConceptId = c.localConceptId WHERE c.conceptId").fetchall()
    except sqlite3.DatabaseError:
        raise AdapterError("home_db_unreadable", "The console's home-screen database couldn't be read.")
    finally:
        db.close()
    return {t: (c, (info or "").startswith(f"/user/appmeta/{t}/logo.png")) for t, c, info in rows if TITLE_ID.fullmatch(t)}


RESTORE_AGAIN_SECONDS = 180  # after a restart ShadowMountPlus adds the games back a while after the console answers
_restoring: set[str] = set()


async def restore(console_id: str):
    """Gives every game on the console's home screen its title logo, unless it still has the one from us. A restart
    of the PS5 registers the games again, which drops their logos, so this runs each time the console comes back
    online: right away and once more RESTORE_AGAIN_SECONDS later. Anything that fails waits for the next time."""
    if console_id in _restoring:
        return
    _restoring.add(console_id)
    try:
        for wait in (0, RESTORE_AGAIN_SECONDS):
            await asyncio.sleep(wait)
            with SessionLocal() as db:
                console = db.get(Console, console_id)
                if not console or console.connection_type not in ("helper", "mock"):  # only the helper sets logos
                    return
                adapter, done = inventory.adapter_for(db, console), []
                try:
                    home_db = await adapter.get_home_db()
                    for title_id, (concept, ours) in home_games(home_db).items():
                        if ours and await adapter.has_file(f"/user/appmeta/{title_id}/logo.png"):
                            continue
                        if not (png := await asyncio.to_thread(cached_logo, concept)):
                            continue
                        if not done:
                            _backup(console, home_db)
                        try:
                            await adapter.set_home_logo(title_id, png)
                            done.append(title_id)
                        except AdapterError as e:
                            if e.code != "not_found":  # not_found: left the home screen meanwhile
                                raise
                except AdapterError:
                    pass  # offline again, or the helper is missing or too old
                if done:  # one entry, not one per game after every restart
                    db.add(AuditLog(action="home_logo", console_id=console_id, result="success", details={
                        "message": f"Put back the logo of {len(done)} game(s) on the home screen.",
                        "title_ids": done, "automatic": True}))
                    db.commit()
    finally:
        _restoring.discard(console_id)
