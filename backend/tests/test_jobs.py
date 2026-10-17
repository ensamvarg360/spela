"""Install engine: FTP and helper staging into ShadowMountPlus folders and the job runner, against a real local FTP server."""
import asyncio
import subprocess
import threading
import zipfile
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import EXFAT, PARAM_JSON, write
from fastapi.testclient import TestClient
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer
from sqlalchemy import delete, select
from test_console_profiles import login
from test_inventory import FakeHelper

from app import __version__, archives, config, credentials, inventory, jobs
from app.consoles import AdapterError, DriveInfo, FTPConsoleAdapter, HelperConsoleAdapter, InstalledGameInfo, RemovalResult
from app.database import Base, SessionLocal, engine
from app.main import app, console_app_token
from app.models import AuditLog, Console, Drive, Game, InstalledGame, InstallJob


@pytest.fixture(autouse=True)
def forget_consoles():
    """The test database is shared; other tests expect no consoles left over."""
    yield
    with SessionLocal() as db:
        for model in (InstallJob, InstalledGame, Drive, Console):
            db.execute(delete(model))
        db.commit()


@pytest.fixture
def ps5(tmp_path, monkeypatch):
    Base.metadata.create_all(engine)  # these tests run jobs without starting the app
    root = tmp_path / "ps5"
    write(root / "user/app/PPSA01111/app.pkg", b"x")
    (root / "data/homebrew").mkdir(parents=True)
    write(root / "mnt/usb0/something", b"x")  # a USB drive without a homebrew folder yet
    auth = DummyAuthorizer()
    auth.add_user("ps5", "pw", str(root), perm="elradfmw")
    handler = type("H", (FTPHandler,), {"authorizer": auth})
    server = FTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.2}, daemon=True).start()
    helper = ThreadingHTTPServer(("127.0.0.1", 0), FakeHelper)  # reports free space
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    monkeypatch.setattr(FTPConsoleAdapter, "HELPER_PORT", helper.server_address[1])
    monkeypatch.setattr(FTPConsoleAdapter, "BLOCK", 16)  # many chunks from tiny files

    lib = tmp_path / "lib"
    write(lib / "Dump PPSA03333/sce_sys/param.json", PARAM_JSON.replace(b"PPSA02222", b"PPSA03333"))
    write(lib / "Dump PPSA03333/eboot.bin", b"e" * 100)
    write(lib / "Dump PPSA03333/sce_sys/about/right.sprx", b"s" * 50)
    write(lib / "Racer PPSA04444.exfat", EXFAT)
    yield root, server.socket.getsockname()[1], lib
    server.close_all()
    helper.shutdown()


def adapter(port):
    return FTPConsoleAdapter(Console(ip_address="127.0.0.1", port=port, username="ps5"), "pw")


INTERNAL = DriveInfo("internal", "Internal SSD", "/user", "internal", 0, 0)
USB = DriveInfo("usb0", "USB drive 0", "/mnt/usb0", "usb", 0, 0)


def test_stage_folder_dump_and_image(ps5):
    root, port, lib = ps5
    a, progress = adapter(port), []
    r = asyncio.run(a.stage_shadowmount_content(str(lib / "Dump PPSA03333"), INTERNAL, lambda d, t: progress.append((d, t))))
    assert r.ok and r.installed.installed_path == "/data/homebrew/Dump PPSA03333"
    assert (root / "data/homebrew/Dump PPSA03333/sce_sys/about/right.sprx").read_bytes() == b"s" * 50
    assert progress[-1] == (len(PARAM_JSON) + 150, len(PARAM_JSON) + 150) and len(progress) > 3
    assert [p.name for p in (root / "data/homebrew").iterdir()] == ["Dump PPSA03333"]  # no .partial left

    asyncio.run(a.stage_shadowmount_content(str(lib / "Racer PPSA04444.exfat"), USB, lambda d, t: None))
    assert (root / "mnt/usb0/homebrew/Racer PPSA04444.exfat").read_bytes() == EXFAT

    with pytest.raises(AdapterError) as e:
        asyncio.run(a.stage_shadowmount_content(str(lib / "Racer PPSA04444.exfat"), USB, lambda d, t: None))
    assert e.value.code == "already_installed"


def test_stage_cancel_and_leftovers(ps5):
    root, port, lib = ps5
    write(root / "data/homebrew/.Dump PPSA03333.partial/old", b"stale")  # an interrupted earlier attempt
    stop = threading.Event()

    def progress(done, total):
        if done > 60:
            stop.set()

    with pytest.raises(AdapterError) as e:
        asyncio.run(adapter(port).stage_shadowmount_content(str(lib / "Dump PPSA03333"), INTERNAL, progress, stop))
    assert e.value.code == "canceled"
    assert list((root / "data/homebrew").iterdir()) == []  # partial cleaned, stale one too

    with pytest.raises(ValueError):  # the cleanup refuses anything that isn't our own partial upload
        FTPConsoleAdapter._rm_partial(None, "/data/homebrew/Real Game")


class FileHelper(FakeHelper):
    """FakeHelper plus the helper's file API (payload/main.c), over a folder laid out like the PS5."""
    version, root = __version__, None

    def _target(self):
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        return url.path, self.root / q["p"][0].lstrip("/"), q

    def do_GET(self):
        if self.path == "/info":
            return super().do_GET()
        op, p, _ = self._target()
        if op == "/ls" and p.is_dir():
            return self._send([{"name": e.name, "dir": e.is_dir(), "size": e.stat().st_size} for e in p.iterdir()])
        if op == "/file" and p.is_file():
            return self._send(p.read_bytes())
        self._send({"error": "not found"}, 404)

    def do_PUT(self):
        _, p, _ = self._target()
        p.write_bytes(self.rfile.read(int(self.headers["Content-Length"])))
        self._send({"ok": True})

    def do_POST(self):
        op, p, q = self._target()
        try:
            if op == "/mkdir":
                p.mkdir()
            elif op == "/rename":
                p.rename(self.root / q["to"][0].lstrip("/"))
            elif op == "/rm":
                p.rmdir() if p.is_dir() else p.unlink()
            self._send({"ok": True})
        except OSError as e:
            self._send({"error": str(e)}, 500)


def test_helper_adapter_stages_scans_and_removes(ps5, monkeypatch):
    root, _, lib = ps5
    FileHelper.root = root
    server = ThreadingHTTPServer(("127.0.0.1", 0), FileHelper)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(FTPConsoleAdapter, "HELPER_PORT", server.server_address[1])
    a = HelperConsoleAdapter(Console(ip_address="127.0.0.1"))
    try:
        assert asyncio.run(a.connect()).state == "online"
        asyncio.run(a.stage_shadowmount_content(str(lib / "Dump PPSA03333"), INTERNAL, lambda d, t: None))
        assert (root / "data/homebrew/Dump PPSA03333/sce_sys/about/right.sprx").read_bytes() == b"s" * 50
        asyncio.run(a.stage_shadowmount_content(str(lib / "Racer PPSA04444.exfat"), USB, lambda d, t: None))
        assert (root / "mnt/usb0/homebrew/Racer PPSA04444.exfat").read_bytes() == EXFAT

        games = {g.title_id: g for g in asyncio.run(a.scan_installed_games())}
        assert {"PPSA01111", "PPSA03333", "PPSA04444"} <= games.keys()
        assert asyncio.run(a.remove_shadowmount_source(games["PPSA03333"], lambda d, t: None)).ok
        assert list((root / "data/homebrew").iterdir()) == []

        stop = threading.Event()
        with pytest.raises(AdapterError, match="Canceled"):
            asyncio.run(a.stage_shadowmount_content(str(lib / "Dump PPSA03333"), INTERNAL,
                                                    lambda d, t: d > 60 and stop.set(), stop))
        assert list((root / "data/homebrew").iterdir()) == []  # partial cleaned up

        # an older helper has no file API: degraded with a reason, never an empty console
        monkeypatch.setattr(FileHelper, "version", "0.0.1")
        status = asyncio.run(a.connect())
        assert (status.state, status.helper_status["helper"]) == ("degraded", "0.0.1")
        with pytest.raises(AdapterError, match="needs"):
            asyncio.run(a.scan_installed_games())
    finally:
        server.shutdown()


def _setup(db, port, lib, *formats):
    c = Console(name="PS5", ip_address="127.0.0.1", connection_type="ftp", port=port, username="ps5")
    db.add(c)
    credentials.set_password(db, c, "pw")
    games = [Game(title=p.name, title_id=None, format=f, source_path=str(p), source_fingerprint="x",
                  size_bytes=200, compatibility_status="ready") for p, f in formats]
    db.add_all(games)
    db.commit()
    asyncio.run(inventory.refresh(db, c))  # creates the drive rows
    inventory.record(c.id, "online")
    return c, games


def test_job_runs_to_success_then_blocks_reinstall(ps5):
    root, port, lib = ps5
    with SessionLocal() as db:
        c, (dump, pkg) = _setup(db, port, lib, (lib / "Dump PPSA03333", "folder_dump"), (lib / "x.pkg", "ps5_pkg"))
        dump.title_id = "PPSA03333"
        db.commit()
        assert "package installer" in jobs.blocker(db, c, pkg)

        job = jobs.queue(db, c, dump)
        asyncio.run(jobs.run(job.id))
        db.expire_all()
        assert (job.status, job.progress, job.bytes_transferred) == ("success", 1.0, len(PARAM_JSON) + 150), job.message
        # largest free space wins: usb0 (612 GiB) over internal (200 GiB)
        assert db.get(Drive, job.target_drive_id).external_id == "usb0"
        assert (root / "mnt/usb0/homebrew/Dump PPSA03333/eboot.bin").exists()
        assert db.scalar(select(InstalledGame).where(InstalledGame.console_id == c.id, InstalledGame.title_id == "PPSA03333"))
        assert jobs.blocker(db, c, dump) == "Already installed on this console."


def test_pkg_firmware_gate():
    ps5, ps4 = Game(format="ps5_pkg"), Game(format="ps4_pkg")
    assert "firmware is unknown" in jobs.pkg_blocker(Console(firmware=None), ps5)
    assert "don't run on firmware 13.60" in jobs.pkg_blocker(Console(firmware="13.60"), ps5)
    assert "PS4 packages don't run on firmware 12.00" in jobs.pkg_blocker(Console(firmware="12.00"), ps4)
    assert "Can't read" in jobs.pkg_blocker(Console(firmware="beta"), ps5)
    for fw in ("4.03", "11.60"):  # in range: only the missing installer blocks it
        assert "no package installer" in jobs.pkg_blocker(Console(firmware=fw), ps5)


def test_job_failures(ps5, monkeypatch):
    root, port, lib = ps5
    with SessionLocal() as db:
        c, (image,) = _setup(db, port, lib, (lib / "Racer PPSA04444.exfat", "image"))
        image.size_bytes = 10 * 1024**4  # 10 TiB: fits nowhere
        job = jobs.queue(db, c, image)
        with pytest.raises(ValueError, match="Already queued"):
            jobs.queue(db, c, image)
        asyncio.run(jobs.run(job.id))
        db.expire_all()
        assert (job.status, job.error_detail["error"]) == ("failed", "no_space")

        image.size_bytes = 10

        def drop(*args):
            raise AdapterError("connection_lost", "The FTP connection failed.")
        monkeypatch.setattr(FTPConsoleAdapter, "_stage_or_clean", lambda self, *a: drop())
        job = jobs.queue(db, c, image)
        asyncio.run(jobs.run(job.id))
        db.expire_all()
        assert job.status == "interrupted"

        job = jobs.queue(db, c, image)
        assert jobs.cancel(db, job) and job.status == "canceled"
        assert not jobs.cancel(db, job)

        job = jobs.queue(db, c, image)
        job.status = "transferring"  # the app stopped mid-copy
        db.commit()
        jobs.recover()
        db.expire_all()
        assert job.status == "interrupted"


@pytest.mark.skipif(not archives.TAR, reason="needs bsdtar (Windows' tar.exe)")
def test_archive_jobs(ps5, tmp_path):
    root, port, lib = ps5
    dump = lib / "Dump PPSA03333"
    with zipfile.ZipFile(lib / "Packed PPSA09999.zip", "w") as z:  # filename guesses the wrong title ID
        for f in dump.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(lib))
    staged = tmp_path / "7z"
    subprocess.run([archives.TAR, "-cf", str(staged), "--format", "7zip", "-C", str(dump), "."], check=True)
    data = staged.read_bytes()  # a byte-split set; the dump sits at the archive's top level
    write(lib / "Split.7z.001", data[:100])
    write(lib / "Split.7z.002", data[100:])
    with zipfile.ZipFile(lib / "Evil.zip", "w") as z:
        z.writestr("../escaped.txt", "x")
        z.writestr("Dump/sce_sys/param.json", PARAM_JSON)
    with zipfile.ZipFile(lib / "Two.zip", "w") as z:
        z.writestr("A/sce_sys/param.json", PARAM_JSON)
        z.writestr("B.exfat", EXFAT)

    with SessionLocal() as db:
        c, (zipped, split, evil, two) = _setup(db, port, lib, *[(lib / n, "archive") for n in
                                                                ("Packed PPSA09999.zip", "Split.7z.001", "Evil.zip", "Two.zip")])
        zipped.title_id, zipped.metadata_source = "PPSA09999", "filename"
        db.commit()
        job = jobs.queue(db, c, zipped)
        asyncio.run(jobs.run(job.id))
        db.expire_all()
        assert job.status == "success", job.message
        assert (root / "mnt/usb0/homebrew/Dump PPSA03333/sce_sys/about/right.sprx").read_bytes() == b"s" * 50
        assert (zipped.title_id, zipped.metadata_source) == ("PPSA03333", "ps5_param_json")  # read from inside
        assert jobs.blocker(db, c, zipped) == "Already installed on this console."

        job = jobs.queue(db, c, split)
        asyncio.run(jobs.run(job.id))
        db.expire_all()
        assert (job.status, job.error_detail["error"]) == ("failed", "already_installed")  # same game inside
        db.execute(delete(InstalledGame))
        db.commit()
        job = jobs.queue(db, c, split)
        asyncio.run(jobs.run(job.id))
        db.expire_all()
        assert job.status == "success", job.message
        drive = {"internal": "data", "usb0": "mnt/usb0"}[db.get(Drive, job.target_drive_id).external_id]
        assert (root / drive / "homebrew/Split/eboot.bin").read_bytes() == b"e" * 100  # named after the archive

        for game, error in ((evil, "archive_extraction_failed"), (two, "archive_contents")):
            job = jobs.queue(db, c, game)
            asyncio.run(jobs.run(job.id))
            db.expire_all()
            assert (job.status, job.error_detail["error"]) == ("failed", error), job.message
        assert not (config.STAGING_DIR.parent / "escaped.txt").exists()
        assert not list(config.STAGING_DIR.iterdir())  # every job's staging folder is gone

        (lib / "Old.part1.rar").write_bytes(b"Rar!\x1a\x07")
        two.source_path = str(lib / "Old.part1.rar")
        assert "Multi-part RAR" in jobs.blocker(db, c, two)


def test_install_api(ps5):
    _, port, lib = ps5
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        with SessionLocal() as db:
            c, (image,) = _setup(db, port, lib, (lib / "Racer PPSA04444.exfat", "image"))
        r = client.post(f"/api/v1/consoles/{c.id}/install", json={"game_id": image.id})
        assert r.status_code == 201, r.text
        job_id = r.json()["id"]
        assert client.post(f"/api/v1/consoles/{c.id}/install", json={"game_id": image.id}).json()["error"] == "cannot_install"
        assert client.delete(f"/api/v1/consoles/{c.id}").status_code == 409  # job active
        for _ in range(50):  # the app's worker runs it
            j = client.get(f"/api/v1/jobs/{job_id}").json()
            if j["status"] not in jobs.ACTIVE:
                break
            asyncio.run(asyncio.sleep(0.1))
        assert j["status"] == "success", j
        assert client.get(f"/api/v1/consoles/{c.id}/jobs").json()[0]["title"] == "Racer PPSA04444.exfat"
        assert client.post(f"/api/v1/jobs/{job_id}/cancel").status_code == 409


def test_remove_source_allowlist(ps5):
    root, port, lib = ps5
    write(root / "data/homebrew/Dump PPSA03333/sce_sys/param.json", PARAM_JSON.replace(b"PPSA02222", b"PPSA03333"))
    write(root / "data/homebrew/Dump PPSA03333/sce_sys/about/right.sprx", b"s")
    write(root / "mnt/usb0/homebrew/Racer PPSA04444.exfat", EXFAT)
    a, progress = adapter(port), []

    def remove(path, tid):
        return asyncio.run(a.remove_shadowmount_source(InstalledGameInfo(tid, "", None, "shadowmountplus", None, path, path, 0),
                                                       lambda d, t: progress.append((d, t))))

    for path in ("/user/app/PPSA01111", "/data/homebrew", "/data/homebrew/../homebrew/Dump PPSA03333",
                 "/data/homebrew/*", "/data/homebrew/PPSA99787", "/data/homebrew/.Dump PPSA03333.partial"):
        with pytest.raises(AdapterError) as e:
            remove(path, "PPSA03333")
        assert e.value.code == "path_outside_allowlist", path
    with pytest.raises(AdapterError) as e:  # right path, different game
        remove("/data/homebrew/Dump PPSA03333", "PPSA09999")
    assert e.value.code == "not_found"

    dump = len(PARAM_JSON) + 1
    assert remove("/data/homebrew/Dump PPSA03333", "PPSA03333").bytes_freed == dump
    assert progress[0] == (0, dump) and progress[-1] == (dump, dump) and len(progress) == 3  # one step per file
    assert remove("/mnt/usb0/homebrew/Racer PPSA04444.exfat", "PPSA04444").ok
    assert list((root / "data/homebrew").iterdir()) == [] and list((root / "mnt/usb0/homebrew").iterdir()) == []
    assert (root / "user/app/PPSA01111/app.pkg").exists()


def test_uninstall_api(ps5):
    root, port, lib = ps5
    write(root / "mnt/usb0/homebrew/Racer PPSA04444.exfat", EXFAT)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        with SessionLocal() as db:
            c, _ = _setup(db, port, lib)
            rows = {g.title_id: g.id for g in db.scalars(select(InstalledGame).where(InstalledGame.console_id == c.id))}
        url = f"/api/v1/consoles/{c.id}/uninstall"

        p = client.post(f"{url}/preview", json={"installed_game_id": rows["PPSA01111"]}).json()  # package install
        assert not p["allowed"] and "ShadowMountPlus" in p["reason"]
        p = client.post(f"{url}/preview", json={"installed_game_id": rows["PPSA04444"]}).json()
        assert p["allowed"] and p["source_path"] == "/mnt/usb0/homebrew/Racer PPSA04444.exfat" and len(p["warnings"]) == 2

        r = client.post(url, json={"removal_id": p["id"], "confirm_title_id": "PPSA00000"})
        assert r.status_code == 409 and "Type PPSA04444" in r.json()["message"]
        assert (root / "mnt/usb0/homebrew/Racer PPSA04444.exfat").exists()
        assert client.post(url, json={"removal_id": "made-up", "confirm_title_id": "PPSA04444"}).status_code == 409

        r = client.post(url, json={"removal_id": p["id"], "confirm_title_id": "ppsa04444"})
        assert r.status_code == 202, r.text
        for _ in range(50):
            j = client.get(f"/api/v1/removal-jobs/{p['id']}").json()
            if j["status"] != "removing":
                break
            asyncio.run(asyncio.sleep(0.1))
        assert j["status"] == "success", j
        assert (j["progress"], j["bytes_total"], j["tile_left"]) == (1.0, len(EXFAT), False)
        assert "tile is gone" in j["message"]
        assert not (root / "mnt/usb0/homebrew/Racer PPSA04444.exfat").exists()
        assert [g["title_id"] for g in client.get(f"/api/v1/consoles/{c.id}/installed").json()] == ["PPSA01111"]
        assert client.post(url, json={"removal_id": p["id"], "confirm_title_id": "PPSA04444"}).status_code == 409  # used up
        with SessionLocal() as db:
            results = [a.result for a in db.scalars(select(AuditLog).where(AuditLog.action == "uninstall").order_by(AuditLog.created_at))]
        assert results[-4:] == ["blocked", "blocked", "success", "blocked"]


def test_uninstall_tile_left(ps5, monkeypatch):
    """When the helper can't remove the tile, both the PC and the PS5 app are told to delete it on the PS5."""
    root, port, lib = ps5
    write(root / "data/homebrew/Racer PPSA04444.exfat", EXFAT)
    monkeypatch.setattr(FTPConsoleAdapter, "_remove_tile", lambda self, tid: RemovalResult(False, "The helper is too old to do it."))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        with SessionLocal() as db:
            c, _ = _setup(db, port, lib)
            row = db.scalar(select(InstalledGame).where(InstalledGame.console_id == c.id, InstalledGame.title_id == "PPSA04444"))
        p = client.post(f"/api/v1/consoles/{c.id}/uninstall/preview", json={"installed_game_id": row.id}).json()
        assert client.post(f"/api/v1/consoles/{c.id}/uninstall", json={"removal_id": p["id"], "confirm_title_id": "PPSA04444"}).status_code == 202
        auth = {"Authorization": f"Bearer {console_app_token(c.id)}"}
        for _ in range(50):
            text = client.get(f"/api/v1/console-app/removals/{p['id']}", headers=auth).text
            if not text.startswith("removing"):
                break
            asyncio.run(asyncio.sleep(0.1))
        status, tile_left, message = text.rstrip("\n").split("\t")
        assert (status, tile_left) == ("success", "1") and "delete it there" in message and "too old" in message
        assert client.get(f"/api/v1/console-app/removals/{p['id']}").status_code == 401


def test_orphaned_tile(ps5, monkeypatch):
    """A ShadowMountPlus tile whose files are gone shows as orphaned, and removing it only removes the tile."""
    root, port, lib = ps5
    write(root / "user/app/PPSA05555/mount.lnk", b"/data/homebrew/Gone PPSA05555\n")
    write(root / "user/appmeta/PPSA05555/param.json", PARAM_JSON.replace(b"PPSA02222", b"PPSA05555"))
    write(root / "user/app/PPSA06666/mount.lnk", b"/mnt/usb1/homebrew/Away PPSA06666")  # drive unplugged: not judged
    write(root / "user/app/PPSA07777/mount.lnk", b"/data/homebrew/Here PPSA07777")  # still there: listed from homebrew
    write(root / "data/homebrew/Here PPSA07777/sce_sys/param.json", PARAM_JSON.replace(b"PPSA02222", b"PPSA07777"))
    tiles = []
    monkeypatch.setattr(FTPConsoleAdapter, "_remove_tile", lambda self, tid: tiles.append(tid) or RemovalResult(True, ""))
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        with SessionLocal() as db:
            c, _ = _setup(db, port, lib)
            rows = {g.title_id: g for g in db.scalars(select(InstalledGame).where(InstalledGame.console_id == c.id))}
        assert set(rows) == {"PPSA01111", "PPSA05555", "PPSA07777"}
        orphan = rows["PPSA05555"]
        assert (orphan.status, orphan.source_path, orphan.installed_path) == \
            ("orphaned_icon", "/data/homebrew/Gone PPSA05555", "/user/app/PPSA05555")
        assert rows["PPSA07777"].status == "installed"

        url = f"/api/v1/consoles/{c.id}/uninstall"
        p = client.post(f"{url}/preview", json={"installed_game_id": orphan.id}).json()
        assert p["allowed"] and p["orphaned"] and p["estimated_bytes_freed"] == 0
        assert client.post(url, json={"removal_id": p["id"], "confirm_title_id": "PPSA05555"}).status_code == 202
        for _ in range(50):
            j = client.get(f"/api/v1/removal-jobs/{p['id']}").json()
            if j["status"] != "removing":
                break
            asyncio.run(asyncio.sleep(0.1))
        assert j["status"] == "success", j
        assert tiles == ["PPSA05555"]
        assert (root / "data/homebrew/Here PPSA07777").exists()
