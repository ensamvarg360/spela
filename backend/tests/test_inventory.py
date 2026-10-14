"""FTP adapter + inventory sync against a real local FTP server laid out like a PS5."""
import asyncio
import shutil
import socket
import threading

import pytest
from conftest import PARAM_JSON, sfo, write
from fastapi.testclient import TestClient
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer
from sqlalchemy import select
from test_console_profiles import login

from app.consoles import FTPConsoleAdapter
from app.database import SessionLocal
from app.main import app
from app.models import Console, Drive, Game, InstalledGame


@pytest.fixture
def ps5(tmp_path):
    write(tmp_path / "user/app/PPSA02222/app.pkg", b"x" * 1234)
    write(tmp_path / "user/appmeta/PPSA02222/param.json", PARAM_JSON)  # "Native Game" 01.020.000
    write(tmp_path / "user/app/CUSA00001/app.pkg", b"x" * 10)
    write(tmp_path / "user/appmeta/CUSA00001/param.sfo", sfo({"TITLE_ID": "CUSA00001", "TITLE": "Old Game", "APP_VER": "01.05"}))
    write(tmp_path / "user/app/NPXS40000/app.pkg")  # no appmeta
    write(tmp_path / "user/app/system.dat")  # not a game
    write(tmp_path / "data/homebrew/Dump/sce_sys/param.json", PARAM_JSON.replace(b"PPSA02222", b"PPSA03333"))
    write(tmp_path / "data/homebrew/Racer PPSA04444.ffpkg", b"x" * 99)
    write(tmp_path / "data/homebrew/tool.elf")  # homebrew, not a game
    write(tmp_path / "mnt/usb0/homebrew/Old CUSA00009.exfat", b"x" * 5)

    auth = DummyAuthorizer()
    auth.add_user("ps5", "pw", str(tmp_path), perm="elr")
    handler = type("H", (FTPHandler,), {"authorizer": auth})
    server = FTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.2}, daemon=True).start()
    yield tmp_path, server.socket.getsockname()[1]
    server.close_all()


def adapter(port, password="pw"):
    return FTPConsoleAdapter(Console(ip_address="127.0.0.1", port=port, username="ps5"), password)


def test_ftp_adapter_reads_drives_and_games(ps5):
    _, port = ps5
    a = adapter(port)
    assert asyncio.run(a.connect()).state == "online"
    assert [d.external_id for d in asyncio.run(a.list_drives())] == ["internal", "usb0"]

    games = {g.title_id: g for g in asyncio.run(a.scan_installed_games())}
    assert set(games) == {"PPSA02222", "CUSA00001", "NPXS40000", "PPSA03333", "PPSA04444", "CUSA00009"}
    native = games["PPSA02222"]
    assert (native.title, native.version, native.install_route, native.size_bytes) == ("Native Game", "01.020.000", "package_installer", 1234)
    assert (games["CUSA00001"].title, games["CUSA00001"].version) == ("Old Game", "01.05")
    assert games["NPXS40000"].title == "NPXS40000"  # no metadata: title ID stands in
    dump = games["PPSA03333"]
    assert (dump.install_route, dump.installed_path, dump.source_path) == ("shadowmountplus", "/data/homebrew/Dump", "/data/homebrew/Dump")
    assert games["PPSA04444"].size_bytes == 99
    assert games["CUSA00009"].drive_external_id == "usb0"

    with pytest.raises(Exception, match="isn't available yet"):
        asyncio.run(a.uninstall_package("PPSA02222"))


def test_ftp_adapter_failures(ps5):
    _, port = ps5
    bad = asyncio.run(adapter(port, "wrong").connect())
    assert (bad.state, bad.helper_status["ftp"]) == ("degraded", "auth_failed")
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    assert asyncio.run(adapter(closed).connect()).helper_status["ftp"] == "helper_unreachable"


def test_inventory_sync_via_api(ps5):
    root, port = ps5
    with TestClient(app) as c:
        login(c)
        con = c.post("/api/v1/consoles", json={"name": "Lab", "ip_address": "127.0.0.1", "connection_type": "ftp",
                                               "port": port, "username": "ps5", "password": "pw"}).json()
        url = f"/api/v1/consoles/{con['id']}"
        with SessionLocal() as db:  # a newer copy in the library
            lib = Game(title="Native Game", title_id="PPSA02222", version="01.100.000", format="ps5_pkg",
                       source_path=str(root / "lib.pkg"), source_fingerprint="x")
            db.add(lib)
            db.commit()

        r = c.post(f"{url}/test-connection").json()
        assert (r["state"], r["last_inventory_scan_at"] is not None) == ("online", True)  # came online -> inventory read

        drives = c.get(f"{url}/drives").json()
        assert [(d["external_id"], d["installed_count"], d["is_install_target"]) for d in drives] == [("internal", 5, True), ("usb0", 1, True)]
        installed = {g["title_id"]: g for g in c.get(f"{url}/installed").json()}
        native = installed["PPSA02222"]
        assert (native["game_id"], native["status"], native["library_version"]) == (lib.id, "update_available", "01.100.000")
        assert installed["CUSA00001"]["status"] == "installed" and installed["CUSA00001"]["game_id"] is None

        usb = drives[1]
        assert c.patch(f"{url}/drives/{usb['id']}", json={"is_install_target": False}).json()["is_install_target"] is False

        # Game removed and USB drive unplugged: the game row goes, the drive keeps its setting.
        shutil.rmtree(root / "user/app/CUSA00001")
        shutil.rmtree(root / "mnt/usb0")
        assert c.post(f"{url}/rescan").status_code == 200
        assert "CUSA00001" not in {g["title_id"] for g in c.get(f"{url}/installed").json()}
        assert {d["external_id"]: d["is_install_target"] for d in c.get(f"{url}/drives").json()} == {"internal": True, "usb0": False}

        c.patch(url, json={"password": "wrong"})
        r = c.post(f"{url}/rescan")
        assert (r.status_code, r.json()["error"]) == (502, "auth_failed")

        assert c.delete(url).status_code == 200
        with SessionLocal() as db:
            assert not db.scalars(select(Drive).where(Drive.console_id == con["id"])).all()
            assert not db.scalars(select(InstalledGame).where(InstalledGame.console_id == con["id"])).all()
            db.delete(db.get(Game, lib.id))
            db.commit()


def test_health_check_reads_inventory_once_online():
    from app import inventory

    with SessionLocal() as db:
        con = Console(name="Mock", ip_address="10.0.0.1", connection_type="mock", port=1)
        db.add(con)
        db.commit()
    assert asyncio.run(inventory.check(con.id))["state"] == "online"
    with SessionLocal() as db:
        assert db.get(Console, con.id).firmware == "4.03"
        assert len(db.scalars(select(InstalledGame).where(InstalledGame.console_id == con.id)).all()) == 2
        inventory.forget(db, con.id)
        db.delete(db.get(Console, con.id))
        db.commit()
