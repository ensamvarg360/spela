"""Console passwords and wake keys, encrypted at rest (project.md §16). Plaintext never leaves this
module except to an adapter that needs it; the API only ever says whether one is set."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from . import config
from .consoles import AdapterError
from .models import Console, Secret, _id

# ponytail: the key sits in the data folder next to app.db, so this protects a copied/leaked
# database, not a copied data folder. Set CREDENTIALS_ENCRYPTION_KEY elsewhere for more.
_fernet = Fernet(base64.urlsafe_b64encode(
    hashlib.sha256(b"ps5sf-credentials:" + config.CREDENTIALS_ENCRYPTION_KEY.encode()).digest()))


def set_password(db: Session, console: Console, password: str | None, field: str = "credentials_secret_id"):
    """Stores `password` for the console; empty or None forgets it. `field`: wake_secret_id for its wake key."""
    secret_id = getattr(console, field)
    secret = db.get(Secret, secret_id) if secret_id else None
    if not password:
        if secret:
            db.delete(secret)
        setattr(console, field, None)
        return
    if not secret:
        secret = Secret(id=_id())
        db.add(secret)
        setattr(console, field, secret.id)
    secret.ciphertext = _fernet.encrypt(password.encode()).decode()


def get_password(db: Session, console: Console, field: str = "credentials_secret_id") -> str | None:
    secret_id = getattr(console, field)
    secret = db.get(Secret, secret_id) if secret_id else None
    if not secret:
        return None
    try:
        return _fernet.decrypt(secret.ciphertext).decode()
    except InvalidToken:
        what = "wake key" if field == "wake_secret_id" else "password"
        raise AdapterError("credentials_unreadable",
                           f"The saved {what} can't be decrypted (the secret key changed). Enter it again.")
