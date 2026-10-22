import os
import secrets
from pathlib import Path

_appdata = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share")
DATA_DIR = Path(os.environ.get("DATA_DIR") or _appdata / "Spela")
_old_dir = _appdata / "PS5 Local Storefront"  # the app's former name; move existing data over once
if not os.environ.get("DATA_DIR") and _old_dir.exists() and not DATA_DIR.exists():
    _old_dir.rename(DATA_DIR)
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")
COVERS_DIR = DATA_DIR / "covers"
# Archives are extracted here before they're sent to the console; it needs room for the unpacked game.
STAGING_DIR = Path(os.environ.get("STAGING_DIR") or DATA_DIR / "staging")
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).parent / "static"))
SAVE_BACKUP_DIR = DATA_DIR / "save-backups"  # copies of save data read from consoles, one folder per title ID
PASSWORD_FILE = DATA_DIR / "admin_password"  # delete it to reset the password
VIEWER_PASSWORD_FILE = DATA_DIR / "viewer_password"  # optional view-only login, set in Settings; delete it to turn that off
SETUP_DISMISSED_FILE = DATA_DIR / "setup_dismissed"  # the Dashboard's setup checklist was closed

# 127.0.0.1 = this PC only; 0.0.0.0 = also phones etc. on your LAN (never the internet).
# The tray option "Allow other devices on my network" creates LAN_FILE; the HOST variable overrides it.
LAN_FILE = DATA_DIR / "allow_other_devices"
HOST = os.environ.get("HOST") or ("0.0.0.0" if LAN_FILE.exists() else "127.0.0.1")
PORT = int(os.environ.get("PORT", "8787"))
# Names the app may be opened by besides IP addresses and localhost (e.g. "tower.local,spela.home.lan"); any other
# name is refused, against DNS rebinding (main.check_host).
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
DOCKER = bool(os.environ.get("SPELA_DOCKER"))  # set in the Docker image: messages talk about the container, not a tray icon
RESTART = "restart the container" if DOCKER else "restart Spela from its tray icon"


def _secret_key() -> str:
    path = DATA_DIR / "secret_key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48))
    return path.read_text().strip()


SECRET_KEY = os.environ.get("SECRET_KEY") or _secret_key()
# Console passwords are encrypted with a key derived from this. Changing it makes saved passwords unreadable.
CREDENTIALS_ENCRYPTION_KEY = os.environ.get("CREDENTIALS_ENCRYPTION_KEY") or SECRET_KEY

SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", "60"))  # 0 disables
CONSOLE_CHECK_SECONDS = int(os.environ.get("CONSOLE_CHECK_SECONDS", "15"))  # console health checks; 0 disables
