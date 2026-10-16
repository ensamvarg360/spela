"""Title logos on the PS5 home screen. A backup's home-screen page shows its name in plain text: the console gets the
logo from PlayStation Network, which a jailbroken console doesn't reach. This PC looks it up in Sony's catalog by the
game's concept ID, read from the console's own database, and the helper puts it on the console."""
import asyncio
import json
import sqlite3
import urllib.request

from sqlalchemy.orm import Session

from . import config, inventory
from .consoles import AdapterError
from .models import Console

CATALOG = "https://m.np.playstation.com/api/catalog/v2/concepts/{}?country=US&language=en-US&age=99"  # no login needed
IMAGES = "https://image.api.playstation.com/"
PNG = b"\x89PNG\r\n\x1a\n"
MAX_BYTES = 16 << 20  # the helper's limit


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
