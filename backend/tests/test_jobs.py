"""Install engine: FTP and helper staging into ShadowMountPlus folders and the job runner, against a real local FTP server."""
import asyncio
import shutil
import subprocess
import threading
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
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
from app.models import AuditLog, Console, Drive, Game, InstalledGame, InstallJob, LibraryRoot


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


def test_stage_deletes_a_copy_that_came_out_short(ps5, monkeypatch):
    """A file the console stored short fails the install before ShadowMountPlus sees it, and nothing is left behind."""
    import ftplib
    import io
    root, port, lib = ps5
    real = ftplib.FTP.storbinary

    def short(self, cmd, fp, *args, **kwargs):
        return real(self, cmd, io.BytesIO(fp.read()[:-5]) if cmd.endswith("eboot.bin") else fp, *args, **kwargs)
    monkeypatch.setattr(ftplib.FTP, "storbinary", short)
    with pytest.raises(AdapterError) as e:
        asyncio.run(adapter(port).stage_shadowmount_content(str(lib / "Dump PPSA03333"), INTERNAL, lambda d, t: None))
    assert e.value.code == "verify_failed" and "eboot.bin" in e.value.message
    assert list((root / "data/homebrew").iterdir()) == []


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

        write(root / "data/homebrew/PPSA99787/assets/version.txt", b"1")
        asyncio.run(a.remove_console_app())  # the test helper has no /uninstall: the folder goes, the tile is left
        asyncio.run(a.remove_console_app())  # already gone: still fine
        assert list((root / "data/homebrew").iterdir()) == []

        stop = threading.Event()
        with pytest.raises(AdapterError, match="Canceled"):
            asyncio.run(a.stage_shadowmount_content(str(lib / "Dump PPSA03333"), INTERNAL,
                                                    lambda d, t: d > 60 and stop.set(), stop))
        assert list((root / "data/homebrew").iterdir()) == []  # partial cleaned up

        write(root / "user/home/1a2b3c4d/savedata_prospero/PPSA03333/sdimg_SAVE0", b"v" * 3000)
        write(root / "user/home/1a2b3c4d/savedata_prospero_meta/PPSA03333", b"meta")  # a file, not a folder
        write(root / "user/home/1a2b3c4d/savedata_prospero/PPSA09999/sdimg_SAVE0", b"other game")
        dest = root.parent / "backup"
        assert asyncio.run(a.backup_saves("PPSA03333", dest, lambda d, t: None)) == 2
        assert (dest / "user/home/1a2b3c4d/savedata_prospero/PPSA03333/sdimg_SAVE0").read_bytes() == b"v" * 3000
        assert (dest / "user/home/1a2b3c4d/savedata_prospero_meta/PPSA03333").read_bytes() == b"meta"
        assert not (dest / "user/home/1a2b3c4d/savedata_prospero/PPSA09999").exists()
        with pytest.raises(AdapterError, match="no save data"):
            asyncio.run(a.backup_saves("PPSA04444", dest, lambda d, t: None))

        # an older helper has no file API: degraded with a reason, never an empty console
        monkeypatch.setattr(FileHelper, "version", "0.0.1")
        status = asyncio.run(a.connect())
        assert (status.state, status.helper_status["helper"]) == ("degraded", "0.0.1")
        with pytest.raises(AdapterError, match="needs"):
            asyncio.run(a.scan_installed_games())
    finally:
        server.shutdown()


class InstallingHelper(FileHelper):
    """FileHelper plus POST /install (payload/main.c, install_pkg): the 'console' unpacks the package into /user/app."""
    asked, installs = [], True

    def do_POST(self):
        op, _, q = self._target()
        if op != "/install":
            return super().do_POST()
        self.asked.append((q["p"][0], q["cid"][0]))
        if self.installs:
            write(self.root / "user/app/PPSA02222/app.pkg", b"p" * 300)
        self._send({"ok": True})


def test_package_install_stages_asks_the_helper_and_cleans_up(ps5, monkeypatch):
    from conftest import PARAM_JSON, cnt, ps5_pkg
    root, _, lib = ps5
    cid = "EP0001-PPSA02222_00-EXAMPLEGAME00000"
    write(lib / "x.pkg", ps5_pkg(cnt(cid, {0x2000: PARAM_JSON})))
    InstallingHelper.root, InstallingHelper.asked, InstallingHelper.installs = root, [], True
    server = ThreadingHTTPServer(("127.0.0.1", 0), InstallingHelper)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(FTPConsoleAdapter, "HELPER_PORT", server.server_address[1])
    monkeypatch.setattr(FTPConsoleAdapter, "INSTALL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(FTPConsoleAdapter, "INSTALL_STALL_SECONDS", 0.5)
    a = HelperConsoleAdapter(Console(ip_address="127.0.0.1"))
    try:
        r = asyncio.run(a.install_package(str(lib / "x.pkg"), lambda d, t: None, INTERNAL))
        assert (r.ok, r.installed.title_id, r.installed.install_route) == (True, "PPSA02222", "package_installer")
        assert InstallingHelper.asked == [("/data/homebrew/x.pkg", cid)]
        assert list((root / "data/homebrew").iterdir()) == []  # the staged copy is gone

        InstallingHelper.installs = False  # the console accepts it but never installs it
        (root / "user/app/PPSA02222/app.pkg").unlink()
        with pytest.raises(AdapterError) as e:
            asyncio.run(a.install_package(str(lib / "x.pkg"), lambda d, t: None, INTERNAL))
        assert e.value.code == "install_stalled"
        assert (root / "data/homebrew/x.pkg").exists()  # the installer may still be reading it

        write(lib / "junk.pkg", b"junk" * 50)
        with pytest.raises(AdapterError) as e:
            asyncio.run(a.install_package(str(lib / "junk.pkg"), lambda d, t: None, INTERNAL))
        assert e.value.code == "pkg_unreadable"
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


def test_job_runs_to_success_then_blocks_reinstall(ps5, monkeypatch):
    root, port, lib = ps5
    told = []
    monkeypatch.setattr(jobs, "notify", lambda title, message: told.append((title, message)))
    with SessionLocal() as db:
        c, (dump, pkg) = _setup(db, port, lib, (lib / "Dump PPSA03333", "folder_dump"), (lib / "x.pkg", "ps5_pkg"))
        dump.title_id = "PPSA03333"
        db.commit()
        assert jobs.blocker(db, c, pkg) is None  # a package passes the same checks once the firmware is in range

        job = jobs.queue(db, c, dump)
        asyncio.run(jobs.run(job.id))
        db.expire_all()
        assert (job.status, job.progress, job.bytes_transferred) == ("success", 1.0, len(PARAM_JSON) + 150), job.message
        # largest free space wins: usb0 (612 GiB) over internal (200 GiB)
        assert db.get(Drive, job.target_drive_id).external_id == "usb0"
        assert (root / "mnt/usb0/homebrew/Dump PPSA03333/eboot.bin").exists()
        assert db.scalar(select(InstalledGame).where(InstalledGame.console_id == c.id, InstalledGame.title_id == "PPSA03333"))
        assert jobs.blocker(db, c, dump) == "Already installed on this console."
        assert told == [("Dump PPSA03333: installed", "Installed on USB drive 0. All 3 files checked.")]


def test_second_copy_of_a_title_is_never_installed(ps5):
    root, port, lib = ps5
    with SessionLocal() as db:
        c, (v1, v2) = _setup(db, port, lib, (lib / "Dump PPSA03333", "folder_dump"), (lib / "Racer PPSA04444.exfat", "image"))
        v1.title_id, v2.title_id = "PPSA03333", "PPSA03333"
        db.commit()
        first = jobs.queue(db, c, v1)
        with pytest.raises(ValueError, match="Another copy of PPSA03333"):
            jobs.queue(db, c, v2)

        v2.title_id = "PPSA04444"  # queued as another title, then corrected to the same one
        second = jobs.queue(db, c, v2)
        v2.title_id = "PPSA03333"
        db.commit()
        asyncio.run(jobs.run(first.id))
        asyncio.run(jobs.run(second.id))
        db.expire_all()
        assert (first.status, second.status, second.error_detail["error"]) == ("success", "failed", "already_installed")
        assert not (root / "mnt/usb0/homebrew/Racer PPSA04444.exfat").exists()


def test_dlc_unlocker_is_copied_into_the_game_folder(ps5):
    root, port, lib = ps5
    write(lib / "Dump PPSA03333/fakelib/libSceAppContent.sprx", b"original")
    write(lib / "unlocker/dlc_emu.ini", b"[PSAC]\ncontent_id=EP0001-PPSA03333_00-ADDON00000000001\n")
    write(lib / "unlocker/fakelib/libSceAppContent.sprx", b"unlocked")
    with SessionLocal() as db:
        c, (dump, image, dlc) = _setup(db, port, lib, (lib / "Dump PPSA03333", "folder_dump"),
                                       (lib / "Racer PPSA04444.exfat", "image"), (lib / "unlocker", "unlocker"))
        dump.title_id, image.title_id, dlc.title_id, dlc.kind = "PPSA03333", "PPSA04444", "PPSA03333", "dlc"
        db.commit()
        assert "Install the game (PPSA03333)" in jobs.blocker(db, c, dlc)

        asyncio.run(jobs.run(jobs.queue(db, c, dump).id))
        job = jobs.queue(db, c, dlc)  # DLC shares the game's title ID: installed game doesn't block it
        asyncio.run(jobs.run(job.id))
        db.expire_all()
        assert job.status == "success", job.message
        game = root / "mnt/usb0/homebrew/Dump PPSA03333"
        assert (game / "dlc_emu.ini").exists() and (game / "fakelib/libSceAppContent.sprx").read_bytes() == b"unlocked"
        assert (game / "eboot.bin").exists()  # the rest of the game is untouched

        asyncio.run(jobs.run(jobs.queue(db, c, image).id))
        dlc.title_id = "PPSA04444"
        assert "installed as a disc image" in jobs.blocker(db, c, dlc)


def test_pkg_firmware_gate():
    ps5, ps4 = Game(format="ps5_pkg"), Game(format="ps4_pkg")
    assert "firmware is unknown" in jobs.pkg_blocker(Console(firmware=None), ps5)
    assert "don't run on firmware 13.60" in jobs.pkg_blocker(Console(firmware="13.60"), ps5)
    assert "PS4 packages don't run on firmware 12.00" in jobs.pkg_blocker(Console(firmware="12.00"), ps4)
    assert "Can't read" in jobs.pkg_blocker(Console(firmware="beta"), ps5)
    for fw in ("4.03", "11.60"):  # in range: nothing blocks it
        assert jobs.pkg_blocker(Console(firmware=fw), ps5) == ""
    assert "DLC packages" in jobs.blocker(None, Console(firmware="4.03"), Game(format="ps5_pkg", kind="dlc"))


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
        with SessionLocal() as db:
            db.get(Game, image.id).title_id = "PPSA04444"
            db.commit()
        h = client.get(f"/api/v1/games/{image.id}/history").json()
        assert [(i["console"], i["drive"]) for i in h["installed"]] == [("PS5", "USB drive 0")]
        assert [(e["what"], e["result"], e["console"]) for e in h["events"]] == [("install", "success", "PS5")]


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


def test_tile_note_pending():
    """A tile the console won't remove yet goes by itself later: no "delete it there", and the reason comes first."""
    why = "ShadowMountPlus: a game is active or awaiting exit"
    note = jobs.tile_note(RemovalResult(False, why, pending=True))
    assert "delete it there" not in note and note.index(why) < note.index("goes by itself")


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


class CopyingHelper(FakeHelper):
    """The fake helper plus the console-side copy (payload/main.c, copy_start), done at once in the fake console's folder."""
    root, status = None, {"running": False, "done": 0, "total": 0, "error": "", "to": ""}

    def do_GET(self):
        if self.path == "/copy":
            return self._send(CopyingHelper.status)
        super().do_GET()

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path == "/copy":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            src, dst = CopyingHelper.root / q["p"].lstrip("/"), CopyingHelper.root / q["to"].lstrip("/")
            (shutil.copytree if src.is_dir() else shutil.copyfile)(src, dst)
            size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file()) if dst.is_dir() else dst.stat().st_size
            CopyingHelper.status = {"running": False, "done": size, "total": size, "error": "", "to": q["to"]}
        return super().do_POST()


def test_move_game_to_another_drive(ps5, monkeypatch):
    root, port, lib = ps5
    CopyingHelper.root = root
    helper = ThreadingHTTPServer(("127.0.0.1", 0), CopyingHelper)
    threading.Thread(target=helper.serve_forever, daemon=True).start()
    monkeypatch.setattr(FTPConsoleAdapter, "HELPER_PORT", helper.server_address[1])
    monkeypatch.setattr(FTPConsoleAdapter, "MOVE_POLL_SECONDS", 0.01)
    try:
        with SessionLocal() as db:
            c, (dump,) = _setup(db, port, lib, (lib / "Dump PPSA03333", "folder_dump"))
            dump.title_id = "PPSA03333"
            db.commit()
            job = jobs.queue(db, c, dump, db.scalar(select(Drive.id).where(Drive.console_id == c.id, Drive.external_id == "internal")))
            asyncio.run(jobs.run(job.id))
            row = db.scalar(select(InstalledGame).where(InstalledGame.console_id == c.id, InstalledGame.title_id == "PPSA03333"))
            internal, usb = (db.scalar(select(Drive).where(Drive.console_id == c.id, Drive.external_id == e)) for e in ("internal", "usb0"))
            assert jobs.move_blocker(db, row, internal) == "It's already on that drive."

            async def move():
                m = jobs.start_move(db, c, row.id, usb.id)
                assert jobs.removal_blocker(db, row) == "It's being moved to another drive. Wait for that to finish."
                while m["status"] == "moving":
                    await asyncio.sleep(0.01)
                return m
            m = asyncio.run(move())
            assert m["status"] == "success", m["message"]
            assert m["message"].startswith("Moved to USB drive 0.")
            assert (root / "mnt/usb0/homebrew/Dump PPSA03333/sce_sys/about/right.sprx").read_bytes() == b"s" * 50
            assert list((root / "data/homebrew").iterdir()) == [] and [p.name for p in (root / "mnt/usb0/homebrew").iterdir()] == ["Dump PPSA03333"]
            db.expire_all()
            moved = db.scalar(select(InstalledGame).where(InstalledGame.console_id == c.id, InstalledGame.title_id == "PPSA03333"))
            assert db.get(Drive, moved.drive_id).external_id == "usb0"
            assert db.scalar(select(AuditLog).where(AuditLog.action == "move")).result == "success"
    finally:
        helper.shutdown()


def test_move_game_to_pc(ps5, tmp_path):
    """A console-only game is copied into a library folder, added to the library, then deleted from the console."""
    root, port, lib = ps5
    write(root / "data/homebrew/Dump PPSA08642/sce_sys/param.json", PARAM_JSON.replace(b"PPSA02222", b"PPSA08642"))
    write(root / "data/homebrew/Dump PPSA08642/eboot.bin", b"e" * 100)
    pc = tmp_path / "pc"
    pc.mkdir()
    with SessionLocal() as db:
        c, _ = _setup(db, port, lib)
        r = LibraryRoot(path=str(pc))
        db.add(r)
        db.commit()
        try:
            row = db.scalar(select(InstalledGame).where(InstalledGame.console_id == c.id, InstalledGame.title_id == "PPSA08642"))
            assert row.game_id is None

            async def move():
                m = jobs.start_move_to_pc(db, c, row.id, r.id)
                while m["status"] == "moving":
                    await asyncio.sleep(0.01)
                return m
            m = asyncio.run(move())
            assert m["status"] == "success", m["message"]
            assert (pc / "Dump PPSA08642/eboot.bin").read_bytes() == b"e" * 100
            assert [p.name for p in pc.iterdir()] == ["Dump PPSA08642"]  # no .partial left
            assert not (root / "data/homebrew/Dump PPSA08642").exists()
            db.expire_all()
            assert db.scalar(select(Game.title_id).where(Game.source_path == str(pc / "Dump PPSA08642"))) == "PPSA08642"
            assert not db.scalar(select(InstalledGame).where(InstalledGame.console_id == c.id, InstalledGame.title_id == "PPSA08642"))
        finally:
            db.execute(delete(Game).where(Game.source_path.startswith(str(pc))))
            db.delete(r)
            db.commit()


def test_copy_to_pc_cancel_leaves_nothing(ps5, tmp_path):
    root, port, lib = ps5
    write(root / "data/homebrew/Big PPSA03333.exfat", b"x" * 200)
    pc = tmp_path / "pc"
    pc.mkdir()
    stop = threading.Event()

    def progress(done, total):
        if done > 50:
            stop.set()
    with pytest.raises(AdapterError) as e:
        asyncio.run(adapter(port).copy_to_pc("/data/homebrew/Big PPSA03333.exfat", pc / "Big PPSA03333.exfat", progress, stop))
    assert e.value.code == "canceled" and list(pc.iterdir()) == []


def test_save_backup_job(ps5, monkeypatch, tmp_path):
    root, port, lib = ps5
    write(root / "user/home/1a2b3c4d/savedata/CUSA00001/sdimg_SAVEDATA00", b"ps4 save")
    monkeypatch.setattr(config, "SAVE_BACKUP_DIR", tmp_path / "saves")
    with SessionLocal() as db:
        c, _ = _setup(db, port, lib)

        async def backup(title_id):
            b = jobs.start_save_backup(db, c, title_id)
            while b["status"] == "copying":
                await asyncio.sleep(0.01)
            return b
        b = asyncio.run(backup("CUSA00001"))
        assert b["status"] == "success", b["message"]
        [listed] = jobs.list_save_backups("CUSA00001")
        assert (listed["files"], listed["bytes"], listed["name"].endswith(" PS5")) == (1, 8, True)
        assert (tmp_path / "saves/CUSA00001" / listed["name"] / "user/home/1a2b3c4d/savedata/CUSA00001/sdimg_SAVEDATA00").exists()

        b = asyncio.run(backup("PPSA07777"))
        assert b["status"] == "failed" and "no save data" in b["message"]
        assert jobs.list_save_backups("PPSA07777") == []  # nothing half-made left behind


def test_save_restore_job(ps5, monkeypatch, tmp_path):
    """No save on the console: written straight away. A save there: "confirm" and nothing changed, then with overwrite it's
    backed up to this PC first and replaced."""
    root, port, lib = ps5
    save = "user/home/1a2b3c4d/savedata_prospero/PPSA03333"
    write(root / "user/home/1a2b3c4d/savedata_prospero/.keep")
    write(tmp_path / f"saves/PPSA03333/backup/{save}/sdimg_SAVEDATA00", b"backup save")
    write(tmp_path / f"saves/PPSA03333/backup/{save}/sce_sys/param.sfo", b"sfo")
    monkeypatch.setattr(config, "SAVE_BACKUP_DIR", tmp_path / "saves")
    with SessionLocal() as db:
        c, _ = _setup(db, port, lib)

        async def restore(overwrite=False):
            r = jobs.start_save_restore(db, c, "PPSA03333", "backup", overwrite)
            while r["status"] == "copying":
                await asyncio.sleep(0.01)
            return r
        r = asyncio.run(restore())
        assert r["status"] == "success" and r["files"] == 2, r["message"]
        assert (root / save / "sdimg_SAVEDATA00").read_bytes() == b"backup save"

        write(root / save / "sdimg_SAVEDATA00", b"newer save on the console")
        r = asyncio.run(restore())
        assert r["status"] == "confirm" and "already has save data" in r["message"]
        assert (root / save / "sdimg_SAVEDATA00").read_bytes() == b"newer save on the console"

        r = asyncio.run(restore(overwrite=True))
        assert r["status"] == "success", r["message"]
        assert (root / save / "sdimg_SAVEDATA00").read_bytes() == b"backup save"
        assert sorted(p.name for p in (root / save).parent.iterdir()) == [".keep", "PPSA03333"]  # no .partial left
        kept = next(b for b in jobs.list_save_backups("PPSA03333") if b["name"] != "backup")
        assert (Path(kept["path"]) / save / "sdimg_SAVEDATA00").read_bytes() == b"newer save on the console"

        write(root / "mnt/sandbox/PPSA03333_000/app0/eboot.bin", b"e")
        r = asyncio.run(restore(overwrite=True))
        assert r["status"] == "failed" and "is open on the console" in r["message"]


def test_progress_reports_the_current_speed(monkeypatch):
    """Ten seconds fast, then five slow: the rate follows the slowdown instead of averaging over the whole copy."""
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        c = Console(name="Speed", ip_address="127.0.0.1", connection_type="mock", port=1)
        g = Game(title="Speed", format="image", compatibility_status="ready", source_path="/speed", source_fingerprint="speed", size_bytes=2000)
        db.add_all([c, g])
        db.flush()
        job = InstallJob(console_id=c.id, game_id=g.id)
        db.add(job)
        db.commit()
        job_id, gid = job.id, g.id
    done = [100 * i for i in range(1, 11)] + [1000 + 10 * i for i in range(1, 6)]  # 100 B/s, then 10 B/s
    clock = iter(float(t) for t in range(1, len(done) + 1))
    monkeypatch.setattr(jobs.time, "monotonic", lambda: next(clock))
    report = jobs._progress(job_id, 0.0)
    for d in done:
        report(d, 2000)
    with SessionLocal() as db:
        j = db.get(InstallJob, job_id)
        assert j.bytes_transferred == 1050 and 10 <= j.transfer_rate_bps < 30  # the whole-copy average is 70
        assert j.eta_seconds == (2000 - 1050) // j.transfer_rate_bps
        db.delete(j)
        db.delete(db.get(Game, gid))
        db.commit()


def test_leftover_partials_are_listed_and_cleaned_up(ps5, monkeypatch):
    """Hidden .partial copies on any drive are listed and deleted; nothing else is touched, and never while a copy runs."""
    root, port, _ = ps5
    write(root / "data/homebrew/.Dump PPSA03333.partial/sce_sys/param.json", b"p" * 10)
    write(root / "data/homebrew/.Dump PPSA03333.partial/eboot.bin", b"e" * 30)
    write(root / "mnt/usb0/homebrew/.Racer PPSA04444.exfat.partial", b"x" * 7)
    write(root / "data/homebrew/Kept PPSA05555/eboot.bin", b"k")
    write(root / "data/homebrew/.PPSA05555-app0.gc-moving.tmp", b"t")  # ShadowMountPlus's own
    a = adapter(port)
    assert asyncio.run(a.list_leftovers()) == {"/data/homebrew/.Dump PPSA03333.partial": 40,
                                               "/mnt/usb0/homebrew/.Racer PPSA04444.exfat.partial": 7}
    monkeypatch.setattr(a, "_helper", lambda path="/info", method="GET": {"running": True} if path == "/copy" else None)
    with pytest.raises(AdapterError, match="copying"):
        asyncio.run(a.remove_leftovers())
    assert len(asyncio.run(a.list_leftovers())) == 2
    monkeypatch.setattr(a, "_helper", lambda path="/info", method="GET": {"running": False} if path == "/copy" else None)
    assert asyncio.run(a.remove_leftovers()) == 47
    assert asyncio.run(a.list_leftovers()) == {}
    assert (root / "data/homebrew/Kept PPSA05555/eboot.bin").exists()
    assert (root / "data/homebrew/.PPSA05555-app0.gc-moving.tmp").exists()


def test_clean_up_api_waits_for_running_work(ps5, monkeypatch):
    from app.consoles import MockConsoleAdapter
    mock = MockConsoleAdapter()
    mock.leftovers = {"/data/homebrew/.Game.partial": 5120}
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: mock)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        with SessionLocal() as db:
            c = Console(name="PS5", ip_address="127.0.0.1", connection_type="mock", port=1)
            db.add(c)
            db.commit()
            cid = c.id
        url = f"/api/v1/consoles/{cid}/leftovers"
        assert client.get(url).json() == {"bytes": 5120, "paths": ["/data/homebrew/.Game.partial"]}
        jobs.removals["r1"] = {"console_id": cid, "status": "removing"}
        try:
            r = client.post(f"{url}/clean")
        finally:
            jobs.removals.pop("r1")
        assert r.status_code == 409 and "removed" in r.json()["message"]
        assert client.post(f"{url}/clean").json() == {"bytes_freed": 5120}
        assert client.get(url).json()["bytes"] == 0
        with SessionLocal() as db:
            log = db.scalars(select(AuditLog).where(AuditLog.console_id == cid)).all()
            assert [(a.action, a.result) for a in log] == [("leftovers_clean", "success")]
            for a in log:
                db.delete(a)
            db.commit()


def test_in_progress_lists_what_quitting_would_cut_short(ps5, monkeypatch):
    _, port, lib = ps5
    assert jobs.in_progress() == []
    with SessionLocal() as db:
        c, (a, b) = _setup(db, port, lib, (lib / "Racer PPSA04444.exfat", "image"), (lib / "Dump PPSA03333", "folder_dump"))
        db.add_all([InstallJob(console_id=c.id, game_id=a.id, status="transferring"),
                    InstallJob(console_id=c.id, game_id=b.id, status="queued"),
                    InstallJob(console_id=c.id, game_id=b.id, status="failed")])
        db.commit()
        monkeypatch.setitem(jobs.conversions, b.id, {"status": "converting"})
    monkeypatch.setitem(jobs.removals, "r", {"status": "removing", "title": "Old Game"})
    monkeypatch.setitem(jobs.moves, "m", {"status": "success", "title": "Done Game"})  # finished: not listed
    assert jobs.in_progress() == ["Installing Racer PPSA04444.exfat", "Waiting to install Dump PPSA03333",
                                  "Converting Dump PPSA03333 to .exfat", "Removing Old Game"]
