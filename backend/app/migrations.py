"""Schema upgrades for a database made by an older version. create_all makes any missing table with today's columns;
each step brings an existing table one version further. SQLite's user_version counts the steps already applied.

Adding a column to a model? Append a step here too, or existing installs break. Never edit or reorder a released step.
Steps only alter tables that already existed: a brand-new table needs no step, create_all makes it.
ponytail: SQLite only (PRAGMA user_version); swap in Alembic if Postgres ever becomes a real target."""
from sqlalchemy import Connection, Engine, inspect

from . import models  # noqa: F401  (registers the tables on Base.metadata)
from .database import Base


def _columns(c: Connection, table: str) -> set[str]:
    return {r[1] for r in c.exec_driver_sql(f"PRAGMA table_info({table})")}


def _1_games_seen_at(c: Connection):
    if "seen_at" not in _columns(c, "games"):  # builds before migrations added it themselves
        c.exec_driver_sql("ALTER TABLE games ADD COLUMN seen_at DATETIME")
        c.exec_driver_sql("UPDATE games SET seen_at = created_at")  # games already there aren't new


def _2_dlc_kind(c: Connection):
    for table in ("games", "library_roots"):
        if "kind" not in _columns(c, table):  # create_all made the table with it if it was missing
            c.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN kind VARCHAR(8) NOT NULL DEFAULT 'game'")
    c.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_games_kind ON games (kind)")


def _3_console_wake_key(c: Connection):
    if "wake_secret_id" not in _columns(c, "consoles"):
        c.exec_driver_sql("ALTER TABLE consoles ADD COLUMN wake_secret_id VARCHAR(36)")


STEPS = [_1_games_seen_at, _2_dlc_kind, _3_console_wake_key]


def upgrade(engine: Engine):
    fresh = not inspect(engine).has_table("games")
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        version = 0 if not fresh else len(STEPS)  # a new database already has every column
        version = max(version, c.exec_driver_sql("PRAGMA user_version").scalar())
        if version > len(STEPS):
            raise RuntimeError("This data folder was used by a newer version of PS5 Local Storefront. Install that "
                               "version again; an older one could damage the library database.")
        for step in STEPS[version:]:
            step(c)
        c.exec_driver_sql(f"PRAGMA user_version = {len(STEPS)}")
