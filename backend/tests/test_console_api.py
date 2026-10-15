"""The PS5 app's API (token auth, plain-text library, raw covers) and putting the app on the console."""
import asyncio

from fastapi.testclient import TestClient
from PIL import Image
from test_console_profiles import login
from test_jobs import adapter, forget_consoles, ps5  # noqa: F401  (fixtures)

from app import config, consoles, main
from app.database import SessionLocal
from app.main import app, console_app_token
from app.models import Console, Game, InstalledGame


def _console(client, name="PS5"):
    r = client.post("/api/v1/consoles", json={"name": name, "ip_address": "127.0.0.1", "connection_type": "mock", "port": 2121})
    return r.json()["id"]


def test_console_api(tmp_path):
    cover = tmp_path / "c.png"
    Image.new("RGB", (600, 900), "red").save(cover)
    with TestClient(app) as client:
        login(client)
        a, b = _console(client, "A"), _console(client, "B")
        with SessionLocal() as db:
            src = tmp_path / "Racer PPSA04444.exfat"
            src.write_bytes(b"x")
            ready = Game(title="Racer\tTabs", title_id="PPSA04444", version="01.000.000", format="image", source_path=str(src),
                         source_fingerprint="x", size_bytes=1, compatibility_status="ready", cover_path=str(cover))
            pkg = Game(title="Pkg Game", title_id="PPSA05555", format="ps5_pkg", source_path=str(tmp_path / "p.pkg"),
                       source_fingerprint="x", size_bytes=1, compatibility_status="ready")
            have = Game(title="Have It", title_id="PPSA06666", format="image", source_path=str(tmp_path / "h.exfat"),
                        source_fingerprint="x", size_bytes=1, compatibility_status="ready")
            db.add_all([ready, pkg, have, InstalledGame(console_id=a, title_id="PPSA06666", title="Have It",
                                                        installed_path="/data/homebrew/h.exfat", status="installed")])
            db.commit()

        auth = {"Authorization": f"Bearer {console_app_token(a)}"}
        assert client.get("/api/v1/console-app/library").status_code == 401
        forged = console_app_token(a).replace(a, b)  # B's id with A's signature
        assert client.get("/api/v1/console-app/library", headers={"Authorization": f"Bearer {forged}"}).status_code == 401

        rows = {r.split("\t")[0]: r.split("\t") for r in client.get("/api/v1/console-app/library", headers=auth).text.splitlines()}
        assert rows[ready.id] == [ready.id, "Racer Tabs", "PPSA04444", "01.000.000", "1", "installable", "", "1"]
        assert rows[pkg.id][5].startswith("blocked:") and "package installer" in rows[pkg.id][5]
        assert rows[have.id][5] == "installed"

        r = client.get(f"/api/v1/console-app/covers/{ready.id}", headers=auth)
        assert len(r.content) == 256 * 320 * 4 and r.content[:4] == b"\xff\x00\x00\xff"
        assert client.get(f"/api/v1/console-app/covers/{pkg.id}", headers=auth).status_code == 404

        assert client.post(f"/api/v1/console-app/install/{pkg.id}", headers=auth).status_code == 409
        r = client.post(f"/api/v1/console-app/install/{ready.id}", headers=auth)
        assert (r.status_code, r.text) == (200, "Queued.")
        state = next(x for x in client.get("/api/v1/console-app/library", headers=auth).text.splitlines() if x.startswith(ready.id))
        assert state.split("\t")[5] in ("queued", "installing", "installable")  # the worker may already have run it (mock is offline: it fails)
        jobs = client.get(f"/api/v1/consoles/{a}/jobs").json()
        assert [j["console_id"] for j in jobs] == [a] and not client.get(f"/api/v1/consoles/{b}/jobs").json()


def test_install_console_app_endpoint(tmp_path, monkeypatch):
    bundle = tmp_path / "PPSA99787"
    (bundle / "sce_sys").mkdir(parents=True)
    (bundle / "eboot.bin").write_bytes(b"elf")
    monkeypatch.setattr(main, "CONSOLE_APP_DIR", bundle)
    with TestClient(app) as client:
        login(client)
        cid = _console(client)
        monkeypatch.setattr(config, "HOST", "127.0.0.1")
        assert client.post(f"/api/v1/consoles/{cid}/console-app").json()["error"] == "lan_off"
        monkeypatch.setattr(config, "HOST", "0.0.0.0")
        assert client.post(f"/api/v1/consoles/{cid}/console-app").status_code == 200


def test_ftp_console_app_upload(ps5):  # noqa: F811
    root, port, _ = ps5
    a = adapter(port)
    files = {"eboot.bin": b"elf", "sce_sys/param.json": b'{"titleId": "PPSA99787"}', "assets/version.txt": b"9.9.9"}
    asyncio.run(a.install_console_app(files))
    asyncio.run(a.install_console_app(files))  # an update over an existing copy
    assert (root / "data/homebrew/PPSA99787/sce_sys/param.json").exists()
    status = asyncio.run(a.connect()).helper_status
    assert status["console_app"] == "9.9.9"
    assert consoles.CONSOLE_APP_TITLE_ID not in {g.title_id for g in asyncio.run(a.scan_installed_games())}
