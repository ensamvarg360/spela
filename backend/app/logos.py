"""Title logos on the PS5 home screen. A backup's home-screen page shows its name in plain text: the console gets the
logo from PlayStation Network, which a jailbroken console doesn't reach. This PC looks it up in Sony's catalog by the
game's concept ID, read from the console's own database, and the helper puts it on the console."""
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

from PIL import Image
from sqlalchemy.orm import Session

from . import config, inventory
from .consoles import AdapterError
from .models import Console

CATALOG = "https://m.np.playstation.com/api/catalog/v2/concepts/{}?country=US&language=en-US&age=99"  # no login needed
PRODUCT = "https://m.np.playstation.com/api/catalog/v2/products/{}?country={}&language={}&age=99"
# A content ID is sold in its own region's store; English stores first, so a Japanese release gets the English logo.
STORES = (("US", "en-US"), ("GB", "en-GB"), ("JP", "ja-JP"), ("DE", "de-DE"))
CONTENT_ID = re.compile(r"[A-Z]{2}\d{4}-[A-Z]{4}\d{5}_\d{2}-[A-Z0-9]{16}")
IMAGES = "https://image.api.playstation.com/"
PNG = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 16 << 20  # the helper's limit
LOGOS_DIR = config.DATA_DIR / "logos"  # library logos: <content ID>.png, or .none when the store has none
RETRY_SECONDS = 7 * 86400  # a .none is asked about again after this


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


def _logo_url(images: list[dict]) -> str | None:
    return next((i["url"] for i in images if i.get("type") == "LOGO"), None)


def library_logo(content_id: str) -> Path | None:
    """A library game's store logo, found by its content ID, shrunk to tile size and kept in LOGOS_DIR.
    None when the store has none or can't be reached; the tile then shows the title as text."""
    if not CONTENT_ID.fullmatch(content_id):
        return None
    path, none = LOGOS_DIR / f"{content_id}.png", LOGOS_DIR / f"{content_id}.none"
    if path.is_file():
        return path
    if none.is_file() and time.time() - none.stat().st_mtime < RETRY_SECONDS:
        return None
    url = None
    for country, language in STORES:
        try:
            url = _logo_url(json.loads(_get(PRODUCT.format(content_id, country, language)))["media"]["images"])
            break
        except urllib.error.HTTPError:
            continue  # not sold in that store
        except (OSError, ValueError, KeyError, TypeError):
            return None  # offline: ask again next time
    LOGOS_DIR.mkdir(exist_ok=True)
    try:
        img = Image.open(io.BytesIO(_get(url))) if url and url.startswith(IMAGES) else None
        if img:
            img.thumbnail((480, 160))
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")  # tiles load in parallel
            img.save(tmp, "PNG")
            os.replace(tmp, path)
            return path
    except OSError:  # unreachable or not an image
        return None
    none.touch()
    return None


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


async def apply(db: Session, console: Console, title_id: str) -> None:
    """Puts the game's logo on `console`'s home screen. Saves the console's database to the data folder first
    (console-backups/<console id>-app.db), the copy to put back over FTP if the home screen ever breaks."""
    adapter = inventory.adapter_for(db, console)
    home_db = await adapter.get_home_db()
    backup = config.DATA_DIR / "console-backups" / f"{console.id}-app.db"
    backup.parent.mkdir(exist_ok=True)
    backup.write_bytes(home_db)  # ponytail: only the latest copy; keep several if a bad write ever goes unnoticed
    if not (concept := concept_id(home_db, title_id)):
        raise AdapterError("not_found", "The console's home screen has no PlayStation Store entry for this game.")
    png = await asyncio.to_thread(fetch_logo, concept)
    await adapter.set_home_logo(title_id, png)
