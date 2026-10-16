"""The PS5 app's API (token auth, plain-text library, raw covers) and putting the app on the console."""
import asyncio
import json

from fastapi.testclient import TestClient
from PIL import Image
from test_console_profiles import login
from test_jobs import adapter, forget_consoles, ps5  # noqa: F401  (fixtures)

from app import config, consoles, inventory, logos, main
from app.database import SessionLocal
from app.main import app, console_app_token
from app.models import Console, Game, InstalledGame


def _console(client, name="PS5"):
    r = client.post("/api/v1/consoles", json={"name": name, "ip_address": "127.0.0.1", "connection_type": "mock", "port": 2121})
    return r.json()["id"]


def test_console_api(tmp_path):
    cover = tmp_path / "c.png"
    Image.new("RGB", (600, 900), "red").save(cover)
    with TestClient(app, base_url="http://127.0.0.1") as client:
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
        installed = {g["id"]: g["installed"] for g in client.get("/api/v1/games").json()}
        assert installed[have.id] and not installed[ready.id]

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
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        cid = _console(client)
        monkeypatch.setattr(config, "HOST", "127.0.0.1")
        assert client.post(f"/api/v1/consoles/{cid}/console-app").json()["error"] == "lan_off"
        monkeypatch.setattr(config, "HOST", "0.0.0.0")
        assert client.post(f"/api/v1/consoles/{cid}/console-app").status_code == 200


def test_home_logo(tmp_path, monkeypatch):
    png = logos.PNG + b"logo"
    catalog = {"media": {"images": [{"type": "MASTER", "url": "https://image.api.playstation.com/m.png"},
                                    {"type": "LOGO", "url": "https://image.api.playstation.com/l.png"}]}}
    fetched = []

    def get(url):
        fetched.append(url)
        return json.dumps(catalog).encode() if "/concepts/" in url else png
    monkeypatch.setattr(logos, "_get", get)
    mock = consoles.MockConsoleAdapter()
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: mock)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        cid = _console(client, "Living Room")
        with SessionLocal() as db:
            on = Game(title="Quest", title_id="PPSA05678", format="image", source_path=str(tmp_path / "q.exfat"),
                      source_fingerprint="x", size_bytes=1, compatibility_status="ready")
            off = Game(title="Not There", title_id="PPSA07777", format="image", source_path=str(tmp_path / "n.exfat"),
                       source_fingerprint="x", size_bytes=1, compatibility_status="ready")
            db.add_all([on, off, InstalledGame(console_id=cid, title_id="PPSA05678", title="Quest",
                                               installed_path="/data/homebrew/q.exfat", status="installed")])
            db.commit()

        assert client.post(f"/api/v1/games/{off.id}/home-logo").json()["error"] == "not_installed"
        r = client.post(f"/api/v1/games/{on.id}/home-logo").json()
        assert r == [{"console": "Living Room", "ok": True, "message": "Logo set."}]
        assert mock.logos == {"PPSA05678": png}
        assert fetched == [logos.CATALOG.format(5678), "https://image.api.playstation.com/l.png"]  # concept from the console's db
        assert (config.DATA_DIR / "console-backups" / f"{cid}-app.db").read_bytes()[:16] == b"SQLite format 3\0"

        catalog["media"]["images"][1]["url"] = "http://192.168.0.1/evil.png"  # only Sony's image server is fetched
        r = client.post(f"/api/v1/games/{on.id}/home-logo").json()
        assert not r[0]["ok"] and "isn't a usable PNG" in r[0]["message"] and fetched[-1].endswith("/concepts/5678?country=US&language=en-US&age=99")
        catalog["media"]["images"].pop()
        assert "no logo" in client.post(f"/api/v1/games/{on.id}/home-logo").json()[0]["message"]


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
