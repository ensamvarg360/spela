"""Full v1 schema (project.md §6). Enum-like columns are plain strings; allowed
values are listed in comments and validated at the API boundary."""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def _id() -> str:
    return str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite drops the timezone; store UTC and hand it back marked as UTC so the browser doesn't read it as local time."""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value and value.tzinfo else value

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value and not value.tzinfo else value


def _pk():
    return mapped_column(String(36), primary_key=True, default=_id)


def _fk(table: str, nullable=False):
    return mapped_column(String(36), ForeignKey(f"{table}.id"), nullable=nullable, index=True)


class LibraryRoot(Base):
    __tablename__ = "library_roots"
    id: Mapped[str] = _pk()
    path: Mapped[str] = mapped_column(String, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    exclude_patterns: Mapped[list] = mapped_column(JSON, default=list)
    kind: Mapped[str] = mapped_column(String(8), default="game")  # game|dlc: which Settings list it's in; archives in a dlc root count as DLC
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now)


class Game(Base):
    __tablename__ = "games"
    id: Mapped[str] = _pk()
    title: Mapped[str] = mapped_column(String)
    title_id: Mapped[str | None] = mapped_column(String(16), index=True)
    content_id: Mapped[str | None] = mapped_column(String(48))
    metadata_source: Mapped[str] = mapped_column(String(16), default="none")  # ps5_cnt|ps5_param_json|ps4_cnt|ps4_param_sfo|unlocker_ini|sidecar|filename|manual|none
    metadata_confidence: Mapped[str] = mapped_column(String(8), default="unknown")  # high|medium|low|unknown
    parser_warnings: Mapped[list] = mapped_column(JSON, default=list)
    region: Mapped[str | None] = mapped_column(String)
    version: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String(8), default="game", index=True)  # game|dlc
    format: Mapped[str] = mapped_column(String(16))  # folder_dump|image|ps5_pkg|ps4_pkg|archive|unlocker|unknown
    image_subtype: Mapped[str | None] = mapped_column(String(16))  # exfat|ffpkg|ffpfs|ffpfsc
    source_path: Mapped[str] = mapped_column(String, unique=True)
    source_fingerprint: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    compressed_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    cover_path: Mapped[str | None] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(Text)
    genre: Mapped[str | None] = mapped_column(String)
    release_date: Mapped[date | None] = mapped_column(Date)
    owned_copy_type: Mapped[str] = mapped_column(String(16), default="unknown")  # disc|digital|other|unknown
    backup_date: Mapped[date | None] = mapped_column(Date)
    provenance_notes: Mapped[str | None] = mapped_column(Text)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    missing: Mapped[bool] = mapped_column(Boolean, default=False)
    compatibility_status: Mapped[str] = mapped_column(String(16), default="unknown")  # ready|needs_attention|incompatible|unknown
    compatibility_reason: Mapped[str] = mapped_column(Text, default="")
    detection_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now, onupdate=now)
    seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # first opened; until then (for 30 days) it's marked new


class Console(Base):
    __tablename__ = "consoles"
    id: Mapped[str] = _pk()
    name: Mapped[str] = mapped_column(String)
    ip_address: Mapped[str] = mapped_column(String)
    connection_type: Mapped[str] = mapped_column(String(16))  # helper|ftp|http|custom|mock
    port: Mapped[int] = mapped_column(Integer)
    username: Mapped[str | None] = mapped_column(String)
    credentials_secret_id: Mapped[str | None] = mapped_column(String(36))  # -> secrets.id
    wake_secret_id: Mapped[str | None] = mapped_column(String(36))  # -> secrets.id: the Remote Play key that wakes it
    firmware: Mapped[str | None] = mapped_column(String)
    helper_status: Mapped[dict] = mapped_column(JSON, default=dict)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_inventory_scan_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Secret(Base):
    """Encrypted credential (Fernet token). Only app/credentials.py reads or writes these."""
    __tablename__ = "secrets"
    id: Mapped[str] = _pk()
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now)


class Drive(Base):
    __tablename__ = "drives"
    id: Mapped[str] = _pk()
    console_id: Mapped[str] = _fk("consoles")
    external_id: Mapped[str] = mapped_column(String)
    label: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    drive_type: Mapped[str] = mapped_column(String(16), default="unknown")  # internal|usb|network|unknown
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    free_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    is_install_target: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class InstalledGame(Base):
    __tablename__ = "installed_games"
    id: Mapped[str] = _pk()
    console_id: Mapped[str] = _fk("consoles")
    game_id: Mapped[str | None] = _fk("games", nullable=True)
    title_id: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String)
    version: Mapped[str | None] = mapped_column(String)
    install_route: Mapped[str] = mapped_column(String(32), default="unknown")  # package_installer|shadowmountplus|unknown
    drive_id: Mapped[str | None] = _fk("drives", nullable=True)
    installed_path: Mapped[str] = mapped_column(String)
    source_path: Mapped[str | None] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(32), default="unknown")  # installed|update_available|partially_installed|orphaned_icon|unknown
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now, onupdate=now)


class InstallJob(Base):
    __tablename__ = "install_jobs"
    id: Mapped[str] = _pk()
    console_id: Mapped[str] = _fk("consoles")
    game_id: Mapped[str] = _fk("games")
    target_drive_id: Mapped[str | None] = _fk("drives", nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|preflight|extracting|transferring|registering|success|failed|canceled|interrupted
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    bytes_transferred: Mapped[int] = mapped_column(BigInteger, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    transfer_rate_bps: Mapped[int] = mapped_column(BigInteger, default=0)
    eta_seconds: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, default="")
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class RemovalJob(Base):
    __tablename__ = "removal_jobs"
    id: Mapped[str] = _pk()
    console_id: Mapped[str] = _fk("consoles")
    installed_game_id: Mapped[str] = _fk("installed_games")
    status: Mapped[str] = mapped_column(String(32), default="preview")  # preview|awaiting_confirmation|removing|success|failed|canceled
    planned_paths: Mapped[list] = mapped_column(JSON, default=list)
    bytes_freed: Mapped[int] = mapped_column(BigInteger, default=0)
    save_data_retained: Mapped[bool] = mapped_column(Boolean, default=True)
    message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[str] = _pk()
    user_id: Mapped[str | None] = mapped_column(String(36))
    console_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(32), index=True)
    title_id: Mapped[str | None] = mapped_column(String(16))
    target_path: Mapped[str | None] = mapped_column(String)
    result: Mapped[str] = mapped_column(String(16))  # success|failed|blocked|canceled
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=now, index=True)
