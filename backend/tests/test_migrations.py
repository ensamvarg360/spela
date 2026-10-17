import pytest
from sqlalchemy import create_engine

from app import migrations


def _version(engine):
    with engine.connect() as c:
        return c.exec_driver_sql("PRAGMA user_version").scalar()


def test_old_database_is_upgraded(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/old.db")
    with engine.begin() as c:  # games as 0.6.0 made it, before seen_at
        c.exec_driver_sql("CREATE TABLE games (id VARCHAR(36) PRIMARY KEY, created_at DATETIME)")
        c.exec_driver_sql("INSERT INTO games VALUES ('g1', '2026-10-01 00:00:00')")
        c.exec_driver_sql("CREATE TABLE consoles (id VARCHAR(36) PRIMARY KEY, name VARCHAR)")  # before wake keys
    migrations.upgrade(engine)
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT seen_at FROM games").scalar() == "2026-10-01 00:00:00"
        assert c.exec_driver_sql("SELECT kind FROM games").scalar() == "game"
        assert "wake_secret_id" in migrations._columns(c, "consoles")
    assert _version(engine) == len(migrations.STEPS)
    migrations.upgrade(engine)  # running again changes nothing


def test_new_database_skips_steps(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/new.db")
    migrations.upgrade(engine)
    assert "seen_at" in migrations._columns(engine.connect(), "games")
    assert _version(engine) == len(migrations.STEPS)


def test_newer_database_is_refused(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/newer.db")
    migrations.upgrade(engine)
    with engine.begin() as c:
        c.exec_driver_sql(f"PRAGMA user_version = {len(migrations.STEPS) + 1}")
    with pytest.raises(RuntimeError, match="newer version"):
        migrations.upgrade(engine)
