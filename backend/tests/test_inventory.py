"""FTP adapter + inventory sync against a real local FTP server laid out like a PS5."""
import asyncio
import json
import re
import shutil
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import PARAM_JSON, sfo, write
from fastapi.testclient import TestClient
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer
from sqlalchemy import select
from test_console_profiles import login

from app import __version__, config
from app.consoles import HELPER_ELF, HELPER_KEY_HEADER, HELPER_KEY_SLOT, AdapterError, FTPConsoleAdapter, helper_elf
from app.database import SessionLocal
from app.main import app
from app.models import Console, Drive, Game, InstalledGame


@pytest.fixture
def ps5(tmp_path):
    write(tmp_path / "user/app/PPSA02222/app.pkg", b"x" * 1234)
    write(tmp_path / "user/appmeta/PPSA02222/param.json", PARAM_JSON)  # "Native Game" 01.020.000
    write(tmp_path / "user/app/CUSA00001/app.pkg", b"x" * 10)
    write(tmp_path / "user/appmeta/CUSA00001/param.sfo", sfo({"TITLE_ID": "CUSA00001", "TITLE": "Old Game", "APP_VER": "01.05"}))
    write(tmp_path / "user/app/MOUU00001/app.pkg")  # no appmeta
    write(tmp_path / "user/app/NPXS40000/app.pkg")  # system app
    write(tmp_path / "user/app/PPSA03333/mount.lnk", b"/data/homebrew/Dump")  # ShadowMountPlus stub
    write(tmp_path / "user/app/WKAL00001/sce_sys/param.json")  # homebrew launcher
    write(tmp_path / "user/app/system.dat")  # not a game
    write(tmp_path / "data/homebrew/Dump/sce_sys/param.json", PARAM_JSON.replace(b"PPSA02222", b"PPSA03333"))
    write(tmp_path / "data/homebrew/Dump/sce_sys/icon0.png", b"dump icon")
    write(tmp_path / "user/appmeta/CUSA00001/icon0.png", b"old icon")
    write(tmp_path / "data/homebrew/Racer PPSA04444.ffpkg", b"x" * 99)
    write(tmp_path / "data/homebrew/tool.elf")  # homebrew, not a game
    write(tmp_path / "data/homebrew/.PPSA05555-app0.gc-moving.tmp/sce_sys/param.json")  # mid-copy
    write(tmp_path / "mnt/usb0/homebrew/Old CUSA00009.exfat", b"x" * 5)
    (tmp_path / "mnt/usb1").mkdir()  # unused mount point
    (tmp_path / "mnt/ext0").mkdir()

    auth = DummyAuthorizer()
    auth.add_user("ps5", "pw", str(tmp_path), perm="elr")
    # ftpsrv on the PS5 has no NLST
    handler = type("H", (FTPHandler,), {"authorizer": auth, "ftp_NLST": lambda self, path: self.respond("502 Command not recognized.")})
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
    assert set(games) == {"PPSA02222", "CUSA00001", "MOUU00001", "PPSA03333", "PPSA04444", "CUSA00009"}
    native = games["PPSA02222"]
    assert (native.title, native.version, native.install_route, native.size_bytes) == ("Native Game", "01.020.000", "package_installer", 1234)
    assert (games["CUSA00001"].title, games["CUSA00001"].version) == ("Old Game", "01.05")
    assert games["MOUU00001"].title == "MOUU00001"  # no metadata: title ID stands in
    dump = games["PPSA03333"]
    assert (dump.install_route, dump.installed_path, dump.source_path) == ("shadowmountplus", "/data/homebrew/Dump", "/data/homebrew/Dump")
    assert games["PPSA04444"].size_bytes == 99
    assert games["CUSA00009"].drive_external_id == "usb0"

    with pytest.raises(Exception, match="Not available yet"):
        asyncio.run(a.uninstall_package("PPSA02222"))


class FakeHelper(BaseHTTPRequestHandler):
    """The helper payload's API; `version` changes when the fake loader receives a new .elf."""
    version, quits = "0.0.1", 0

    def parse_request(self):  # the real helper answers 401 to any request without its key
        if not super().parse_request():
            return False
        if len(self.headers.get(HELPER_KEY_HEADER, "")) != 64:
            self._send({"error": "wrong key"}, 401)
            return False
        return True

    def do_GET(self):
        G = 1024**3
        self._send({"version": type(self).version, "firmware": "4.03", "model": "CFI-1216A", "mounts": [
            {"mount": "/", "total": 100 * G, "free": 20 * G},
            {"mount": "/user", "total": 800 * G, "free": 200 * G},
            {"mount": "/mnt/usb0", "total": 2000 * G, "free": 612 * G}]})

    def do_POST(self):
        FakeHelper.quits += 1
        self._send({"ok": True})

    def _send(self, body, status=200):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_remove_tile_answers():
    """The helper's answers to /uninstall: tile gone, refused for now (it retries), refused, or too old a helper."""
    class Tile(FakeHelper):
        answer = 200, {"ok": True}

        def do_POST(self):
            self._send(type(self).answer[1], type(self).answer[0])

    helper = ThreadingHTTPServer(("127.0.0.1", 0), Tile)
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    a = adapter(21)
    a.HELPER_PORT = helper.server_address[1]
    why = "ShadowMountPlus: a game is active or awaiting exit; the console: 0x80990088"
    try:
        for answer, want in [((200, {"ok": True}), (True, False, "Tile removed.")),
                             ((202, {"error": why, "pending": True}), (False, True, why)),
                             ((500, {"error": why}), (False, False, why)),
                             ((404, {"error": "not_found"}), (False, False, "The helper is too old to do it."))]:
            Tile.answer = answer
            t = a._remove_tile("PPSA04444")
            assert (t.ok, t.pending, t.message) == want
    finally:
        helper.shutdown()


def test_other_spelas_helper_is_named():
    """A helper holding another Spela's key answers 401 with that Spela's name; older helpers send none."""
    class Theirs(FakeHelper):
        body = {"error": "wrong key", "owner": "Docker · nas"}

        def parse_request(self):
            BaseHTTPRequestHandler.parse_request(self)
            self._send(type(self).body, 401)
            return False

    helper = ThreadingHTTPServer(("127.0.0.1", 0), Theirs)
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    a = adapter(21)
    a.HELPER_PORT = helper.server_address[1]
    try:
        assert a._no_helper() == {"helper": "other_pc", "helper_owner": "Docker · nas"}
        Theirs.body = {"error": "wrong key"}
        assert a._no_helper() == {"helper": "other_pc", "helper_owner": ""}
    finally:
        helper.shutdown()


def test_ftp_adapter_helper(ps5):
    _, port = ps5
    helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelper)
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    loader, received = socket.create_server(("127.0.0.1", 0)), []

    def elfldr():  # reads one payload, then the "new" helper answers
        conn, _ = loader.accept()
        with conn:
            received.append(b"".join(iter(lambda: conn.recv(65536), b"")))
        FakeHelper.version = __version__

    threading.Thread(target=elfldr, daemon=True).start()
    a = adapter(port)
    a.HELPER_PORT, a.LOADER_PORT = helper.server_address[1], loader.getsockname()[1]
    try:
        G = 1024**3
        assert {d.external_id: (d.total_bytes, d.free_bytes) for d in asyncio.run(a.list_drives())} ==             {"internal": (800 * G, 200 * G), "usb0": (2000 * G, 612 * G)}  # "/" never counts as a drive
        info = asyncio.run(a.get_console_info())
        assert (info.firmware, info.helper_status["helper"]) == ("4.03", "0.0.1")

        assert asyncio.run(a.install_helper()) == __version__
        assert received == [helper_elf(a.key)] and FakeHelper.quits == 1  # old copy told to quit first
        assert b"storefront-helper-key:" + a.key.encode() in received[0] and HELPER_KEY_SLOT not in received[0]
        assert b"storefront-helper-owner:" + config.INSTANCE_NAME.encode() + b"\0" in received[0]
        assert asyncio.run(a.connect()).helper_status["helper"] == __version__

        # loader not running: a clear error. Helper gone: drives listed, space and firmware unknown.
        loader.close()
        with pytest.raises(AdapterError, match="ELF loader"):
            asyncio.run(a.install_helper())
    finally:
        helper.shutdown()
        helper.server_close()
    assert {(d.total_bytes, d.free_bytes) for d in asyncio.run(a.list_drives())} == {(0, 0)}
    info = asyncio.run(a.get_console_info())
    assert (info.firmware, info.helper_status["helper"]) == (None, "missing")


def test_bundled_helper_matches_app_version():
    # Fails after a version bump until payload/build.ps1 is run, so the app never ships a helper it doesn't expect.
    m = re.search(rb"storefront-helper-version:([\w.-]+)", HELPER_ELF.read_bytes())
    assert m and m[1].decode() == __version__


def test_ftp_adapter_failures(ps5):
    _, port = ps5
    bad = asyncio.run(adapter(port, "wrong").connect())
    assert (bad.state, bad.helper_status["ftp"]) == ("degraded", "auth_failed")
    with socket.socket() as s:  # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    assert asyncio.run(adapter(closed).connect()).helper_status["ftp"] == "helper_unreachable"

    # rest mode: FTP refused, but the discovery probe answers "620 Server Standby"; "200 Ok" means awake
    for reply, state in ((b"HTTP/1.1 620 Server Standby\nhost-type:PS5\n", "standby"), (b"HTTP/1.1 200 Ok\n", "degraded")):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as ddp:
            ddp.bind(("127.0.0.1", 0))
            a = adapter(closed)
            a.DDP_PORT = ddp.getsockname()[1]
            t = threading.Thread(target=lambda: (lambda d, addr: ddp.sendto(reply, addr))(*ddp.recvfrom(2048)))
            t.start()
            assert asyncio.run(a.connect()).state == state
            t.join()


def test_inventory_sync_via_api(ps5):
    root, port = ps5
    with TestClient(app, base_url="http://127.0.0.1") as c:
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
        # icons read from the console: a dump's sce_sys, else appmeta; none found -> no cover
        assert c.get(installed["CUSA00001"]["cover_url"]).content == b"old icon"
        assert c.get(installed["PPSA03333"]["cover_url"]).content == b"dump icon"
        assert installed["MOUU00001"]["cover_url"] is None
        assert c.get("/api/v1/icons/..%2Fsecret_key").status_code == 404

        usb = drives[1]
        assert c.patch(f"{url}/drives/{usb['id']}", json={"is_install_target": False}).json()["is_install_target"] is False

        # Game removed and USB drive unplugged: the game row goes, the drive is hidden but keeps its setting.
        shutil.rmtree(root / "user/app/CUSA00001")
        shutil.move(root / "mnt/usb0", root / "unplugged")
        assert c.post(f"{url}/rescan").status_code == 200
        assert "CUSA00001" not in {g["title_id"] for g in c.get(f"{url}/installed").json()}
        assert [d["external_id"] for d in c.get(f"{url}/drives").json()] == ["internal"]
        shutil.move(root / "unplugged", root / "mnt/usb0")
        assert c.post(f"{url}/rescan").status_code == 200
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


def test_helper_is_sent_again_after_a_reboot(monkeypatch):
    """A console that's on without the helper gets it sent once its ELF loader is up, at most once a minute."""
    from app import inventory
    from app.consoles import ConsoleStatus, MockConsoleAdapter
    from app.models import AuditLog

    class Rebooted(MockConsoleAdapter):
        sent = 0
        loader_up = False

        async def connect(self):
            if Rebooted.sent:
                return await super().connect()
            return ConsoleStatus("degraded", "The console is on, but the helper isn't running.", {"helper": "missing"})

        async def install_helper(self):
            if not Rebooted.loader_up:
                raise AdapterError("loader_unreachable", "No loader.")
            Rebooted.sent += 1
            return __version__

    with SessionLocal() as db:
        con = Console(name="Rebooted", ip_address="10.0.0.2", connection_type="helper", port=7735)
        db.add(con)
        db.commit()
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: Rebooted(c))
    assert asyncio.run(inventory.check(con.id))["state"] == "degraded"  # loader not up yet
    Rebooted.loader_up = True
    assert asyncio.run(inventory.check(con.id))["state"] == "degraded"  # tried a moment ago: waits a minute
    monkeypatch.setitem(inventory._auto_helper_at, con.id, 0.0)
    monkeypatch.setattr(inventory.time, "monotonic", lambda: 1e9)
    assert asyncio.run(inventory.check(con.id))["state"] == "online"
    assert Rebooted.sent == 1
    with SessionLocal() as db:
        log = db.scalars(select(AuditLog).where(AuditLog.console_id == con.id)).all()
        assert [(a.action, a.details["automatic"]) for a in log] == [("helper_install", True)]
        inventory.forget(db, con.id)
        for a in log:
            db.delete(a)
        db.delete(db.get(Console, con.id))
        db.commit()


def test_another_spelas_helper_is_left_alone(monkeypatch):
    """A helper with another Spela's key (PC and Docker both set up) isn't replaced every minute: they'd take turns."""
    from app import inventory
    from app.consoles import ConsoleStatus, MockConsoleAdapter

    class Taken(MockConsoleAdapter):
        sent = 0

        async def connect(self):
            return ConsoleStatus("degraded", "Another Spela runs it.", {"helper": "other_pc"})

        async def install_helper(self):
            Taken.sent += 1
            return __version__

    with SessionLocal() as db:
        con = Console(name="Shared", ip_address="10.0.0.4", connection_type="helper", port=7735)
        db.add(con)
        db.commit()
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: Taken(c))
    monkeypatch.setattr(inventory.time, "monotonic", lambda: 1e9)
    assert asyncio.run(inventory.check(con.id))["state"] == "degraded" and Taken.sent == 0
    with SessionLocal() as db:
        inventory.forget(db, con.id)
        db.delete(db.get(Console, con.id))
        db.commit()


def test_remove_helper_stops_it_and_its_resending(monkeypatch):
    """Remove helper stops the payload and keeps Spela from sending it again, until Install helper is clicked."""
    from app import inventory
    from app.consoles import ConsoleStatus, MockConsoleAdapter

    class Stoppable(MockConsoleAdapter):
        running, sent = True, 0

        async def connect(self):
            if Stoppable.running:
                return await super().connect()
            return ConsoleStatus("degraded", "Your PS5 is on, but the helper isn't running yet.", {"helper": "missing"})

        async def stop_helper(self):
            Stoppable.running = False

        async def install_helper(self):
            Stoppable.running, Stoppable.sent = True, Stoppable.sent + 1
            return __version__

    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: Stoppable(c))
    monkeypatch.setattr(inventory.time, "monotonic", lambda: 1e9)  # never "tried a moment ago"
    with TestClient(app, base_url="http://127.0.0.1") as c:
        login(c)
        con = c.post("/api/v1/consoles", json={"name": "Leaving", "ip_address": "10.0.0.3", "connection_type": "helper",
                                               "port": 7735}).json()
        url = f"/api/v1/consoles/{con['id']}"
        r = c.delete(f"{url}/helper").json()
        assert not r["auto_helper"] and r["status_message"] == "The helper is removed." and "Install helper" in r["status_fix"]
        assert c.post(f"{url}/test-connection").json()["state"] == "degraded" and Stoppable.sent == 0  # not sent again
        assert c.post(f"{url}/helper").json()["auto_helper"] and Stoppable.sent == 1  # clicked: on again
        c.delete(url)


def test_outdated_console_app_is_updated_by_itself(monkeypatch):
    """An installed PS5 app older than this PC's is copied again; a console without the app never gets it."""
    from app import inventory
    from app.consoles import ConsoleStatus, MockConsoleAdapter
    from app.models import AuditLog

    class WithApp(MockConsoleAdapter):
        app = "0.1.0"
        copied: list = []

        async def connect(self):
            return ConsoleStatus("online", "", {"helper": __version__, "console_app": WithApp.app})

        async def install_console_app(self, files):
            WithApp.copied.append(files["assets/version.txt"].decode())

        async def set_home_logo(self, title_id, png):
            WithApp.logos.append(title_id)
        logos: list = []

    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: WithApp(c))
    monkeypatch.setattr(inventory, "refresh", lambda db, c: asyncio.sleep(0))
    monkeypatch.setattr(inventory.relay, "ensure", lambda *a: None)
    monkeypatch.setattr(inventory.time, "monotonic", lambda: 1e9)
    with SessionLocal() as db:
        con = Console(name="Couch", ip_address="10.0.0.4", connection_type="helper", port=7735)
        db.add(con)
        db.commit()
    asyncio.run(inventory.check(con.id))
    assert WithApp.copied == [__version__]
    assert WithApp.logos == ["PPSA99787"]
    with SessionLocal() as db:
        assert db.get(Console, con.id).helper_status["console_app"] == __version__
    WithApp.app = __version__
    monkeypatch.setitem(inventory._auto_app_at, con.id, 0.0)
    asyncio.run(inventory.check(con.id))
    assert WithApp.logos == ["PPSA99787"]  # already there
    inventory.app_logo_set.discard(con.id)
    WithApp.app = "missing"  # removed, or never installed
    monkeypatch.setitem(inventory._auto_app_at, con.id, 0.0)
    asyncio.run(inventory.check(con.id))
    assert WithApp.copied == [__version__]
    with SessionLocal() as db:
        log = db.scalars(select(AuditLog).where(AuditLog.console_id == con.id)).all()
        assert [(a.action, a.details["automatic"]) for a in log] == [("console_app_install", True)]
        inventory.forget(db, con.id)
        for a in log:
            db.delete(a)
        db.delete(db.get(Console, con.id))
        db.commit()
