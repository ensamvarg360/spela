"""The PS5 app's API (token auth, plain-text library, raw covers) and putting the app on the console."""
import asyncio
import io
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_console_profiles import login
from test_jobs import adapter, forget_consoles, ps5  # noqa: F401  (fixtures)

from app import config, consoles, inventory, jobs, logos, main
from app.database import SessionLocal
from app.main import app, console_app_token
from app.models import AuditLog, Console, Drive, Game, InstalledGame, InstallJob
from sqlalchemy import select


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
            dlc = Game(title="Have It Unlocker", title_id="PPSA06666", kind="dlc", format="unlocker",  # its game's title ID
                       source_path=str(tmp_path / "u"), source_fingerprint="x", size_bytes=1, compatibility_status="ready")
            db.add_all([ready, pkg, have, dlc, InstalledGame(console_id=a, title_id="PPSA06666", title="Have It",
                                                             installed_path="/data/homebrew/h.exfat", status="installed"),
                        Drive(console_id=a, external_id="usb0", label="USB", path="/mnt/usb0", total_bytes=100, free_bytes=40,
                              is_install_target=True),
                        Drive(console_id=a, external_id="int", label="Internal", path="/data", is_install_target=False)])
            db.commit()
            usb = db.scalar(select(Drive.id).where(Drive.label == "USB"))

        auth = {"Authorization": f"Bearer {console_app_token(a)}"}
        assert client.get("/api/v1/console-app/library").status_code == 401
        forged = console_app_token(a).replace(a, b)  # B's id with A's signature
        assert client.get("/api/v1/console-app/library", headers={"Authorization": f"Bearer {forged}"}).status_code == 401

        rows = {r.split("\t")[0]: r.split("\t") for r in client.get("/api/v1/console-app/library", headers=auth).text.splitlines()}
        assert rows[ready.id][:15] == [ready.id, "Racer Tabs", "PPSA04444", "01.000.000", "1", "installable", "", "1", "game", "image", "", "", "", "", ""]
        assert rows[ready.id][15] == "0" and int(rows[ready.id][16]) > 1_700_000_000  # favorite, added
        assert rows[pkg.id][5].startswith("blocked:") and "packages need firmware" in rows[pkg.id][5]
        assert rows[have.id][5] == "installed"
        assert rows[dlc.id][8:10] == ["dlc", "unlocker"] and rows[dlc.id][5] != "installed"  # not its game's state

        blockers = client.get(f"/api/v1/consoles/{a}/blockers", params={"game_ids": f"{ready.id},{pkg.id}"}).json()
        assert list(blockers) == [pkg.id] and "packages need firmware" in blockers[pkg.id]  # installable games are left out
        assert client.get(f"/api/v1/consoles/{a}/firmware-blocked").json() == []  # firmware unknown: nothing flagged
        with SessionLocal() as db:
            db.get(Console, a).firmware = "13.60"
            db.commit()
        assert client.get(f"/api/v1/consoles/{a}/firmware-blocked").json() == [pkg.id]
        with SessionLocal() as db:
            db.get(Console, a).firmware = None
            db.commit()

        # a favorite from the PS5 marks the game's other copies too, not its DLC
        with SessionLocal() as db:
            folder = Game(title="Have It", title_id="PPSA06666", format="folder_dump", source_path=str(tmp_path / "h"),
                          source_fingerprint="x", size_bytes=1, compatibility_status="ready")
            db.add(folder)
            db.commit()
        r = client.post(f"/api/v1/console-app/favorite/{have.id}?on=true", headers=auth)
        assert (r.status_code, r.text) == (200, "Added to Favorites.")
        rows = {r.split("\t")[0]: r.split("\t") for r in client.get("/api/v1/console-app/library", headers=auth).text.splitlines()}
        assert [rows[i][15] for i in (have.id, folder.id, dlc.id, ready.id)] == ["1", "1", "0", "0"]
        client.post(f"/api/v1/console-app/favorite/{folder.id}?on=false", headers=auth)
        assert not any(g["favorite"] for g in client.get("/api/v1/games").json() if g["id"] in (have.id, folder.id))

        assert client.get("/api/v1/console-app/drives", headers=auth).text == f"{usb}\tUSB\t40\t100\n"  # install targets only
        installed = {g["id"]: g["installed"] for g in client.get("/api/v1/games").json()}
        assert installed[have.id] and not installed[ready.id]

        r = client.get(f"/api/v1/console-app/covers/{ready.id}", headers=auth)
        assert len(r.content) == 256 * 320 * 4 and r.content[:4] == b"\xff\x00\x00\xff"
        assert client.get(f"/api/v1/console-app/covers/{pkg.id}", headers=auth).status_code == 404

        assert client.post(f"/api/v1/console-app/install/{pkg.id}", headers=auth).status_code == 409
        gone = client.post("/api/v1/console-app/install/no-such-game", headers=auth)  # the PS5 app shows errors as plain text
        assert gone.status_code == 404 and gone.text == "Game not found."
        assert client.post(f"/api/v1/console-app/install/{ready.id}?drive=made-up", headers=auth).status_code == 409
        r = client.post(f"/api/v1/console-app/install/{ready.id}?drive={usb}", headers=auth)
        assert (r.status_code, r.text) == (200, "Queued.")
        with SessionLocal() as db:
            assert db.scalar(select(InstallJob.target_drive_id).where(InstallJob.game_id == ready.id)) == usb
            db.add(InstallJob(console_id=a, game_id=have.id, status="transferring", progress=0.25, bytes_transferred=25,
                              total_bytes=100, transfer_rate_bps=5, eta_seconds=15, message="Copying to USB."))  # running: the worker leaves it alone
            db.commit()
        row = next(x for x in client.get("/api/v1/console-app/library", headers=auth).text.splitlines() if x.startswith(have.id))
        assert row.split("\t")[5:7] + row.split("\t")[10:15] ==["installing", "0.250", "25", "100", "5", "15", "Copying to USB."]
        assert client.post(f"/api/v1/console-app/install/{have.id}/cancel", headers=auth).text == "Stopping."
        assert client.post(f"/api/v1/console-app/install/{have.id}/cancel", headers=auth).status_code == 200  # still running
        assert client.post(f"/api/v1/console-app/install/{pkg.id}/cancel", headers=auth).status_code == 409
        state = next(x for x in client.get("/api/v1/console-app/library", headers=auth).text.splitlines() if x.startswith(ready.id))
        assert state.split("\t")[5] in ("queued", "installing", "installable")  # the worker may already have run it (mock is offline: it fails)
        jobs = client.get(f"/api/v1/consoles/{a}/jobs").json()
        assert {j["console_id"] for j in jobs} == {a} and not client.get(f"/api/v1/consoles/{b}/jobs").json()


def test_install_console_app_endpoint(tmp_path, monkeypatch):
    bundle = tmp_path / "PPSA99787"
    (bundle / "sce_sys").mkdir(parents=True)
    (bundle / "eboot.bin").write_bytes(b"elf")
    monkeypatch.setattr(main, "CONSOLE_APP_DIR", bundle)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        cid = _console(client)
        sent = {}
        real = consoles.MockConsoleAdapter.install_console_app

        async def keep(self, files):
            sent.update(files)
            await real(self, files)
        monkeypatch.setattr(consoles.MockConsoleAdapter, "install_console_app", keep)
        monkeypatch.setattr(config, "HOST", "127.0.0.1")  # other devices not allowed: the PS5 app still works
        assert client.post(f"/api/v1/consoles/{cid}/console-app").status_code == 200
        # it asks the helper on its own console, which passes its requests on to the PC (relay.py)
        assert sent["assets/server.txt"].decode().startswith("url=http://127.0.0.1:7735\ntoken=")


def test_home_logo(tmp_path, monkeypatch):
    png = logos.PNG + b"logo"
    monkeypatch.setattr(logos, "STORE_DIR", tmp_path)  # apply() keeps the logo there
    catalog = {"media": {"images": [{"type": "MASTER", "url": "https://image.api.playstation.com/m.png"},
                                    {"type": "LOGO", "url": "https://image.api.playstation.com/l.png"}]}}
    fetched = []

    def get(url):
        fetched.append(url)
        return json.dumps(catalog).encode() if "/concepts/" in url else png
    monkeypatch.setattr(logos, "_get", get)
    mock = consoles.MockConsoleAdapter()
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: mock)
    with SessionLocal() as db:
        con = Console(name="Living Room", ip_address="127.0.0.1", connection_type="mock", port=2121)
        db.add(con)
        db.commit()
        asyncio.run(logos.apply(db, con, "PPSA05678"))
        assert mock.logos == {"PPSA05678": png}
        assert fetched == [logos.CATALOG.format(5678), "https://image.api.playstation.com/l.png"]  # concept from the console's db
        assert (config.DATA_DIR / "console-backups" / f"{con.id}-app.db").read_bytes()[:16] == b"SQLite format 3\0"

        catalog["media"]["images"][1]["url"] = "http://192.168.0.1/evil.png"  # only Sony's image server is fetched
        with pytest.raises(consoles.AdapterError, match="isn't a usable PNG"):
            asyncio.run(logos.apply(db, con, "PPSA05678"))
        assert fetched[-1].endswith("/concepts/5678?country=US&language=en-US&age=99")
        catalog["media"]["images"].pop()
        with pytest.raises(consoles.AdapterError, match="no logo"):
            asyncio.run(logos.apply(db, con, "PPSA05678"))


def test_push_home_logo(tmp_path, monkeypatch):
    png = logos.PNG + b"logo"
    monkeypatch.setattr(logos, "STORE_DIR", tmp_path)
    monkeypatch.setattr(logos, "_get", lambda url: json.dumps(
        {"media": {"images": [{"type": "LOGO", "url": "https://image.api.playstation.com/l.png"}]}}).encode()
        if "/concepts/" in url else png)
    mock = consoles.MockConsoleAdapter()
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: mock)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        cid = _console(client)
        r = client.post(f"/api/v1/consoles/{cid}/home-logo", json={"title_id": "PPSA05678"})
        assert r.status_code == 200 and mock.logos["PPSA05678"] == png
        assert (tmp_path / "concept-5678.png").read_bytes() == png  # what restore() puts back after a restart
        r = client.post(f"/api/v1/consoles/{cid}/home-logo", json={"title_id": "PPSA09999"})  # not on the home screen
        assert r.status_code == 502 and r.json()["error"] == "not_found"
    with SessionLocal() as db:
        events = db.scalars(select(AuditLog).where(AuditLog.action == "home_logo")).all()
        assert [e.result for e in events[-2:]] == ["success", "failed"]


def test_logos_back_after_restart(tmp_path, monkeypatch):
    """Each time the console comes online, every game gets the logo picked for it where the console's differs."""
    png, own = logos.PNG + b"logo", logos.PNG + b"own"
    fetched = []

    def get(url):
        fetched.append(url)
        return json.dumps({"media": {"images": [{"type": "LOGO", "url": "https://image.api.playstation.com/l.png"}]}}).encode()             if "/concepts/" in url else png
    monkeypatch.setattr(logos, "_get", get)
    monkeypatch.setattr(logos, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(config, "COVERS_DIR", tmp_path / "covers")
    mock = consoles.MockConsoleAdapter()
    mock.logos["PPSA01234"] = png  # still there
    reads = []
    read_file = mock.read_file
    mock.read_file = lambda path: reads.append(path) or read_file(path)
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: mock)

    def sweeps():
        with SessionLocal() as db:
            return [e.details["title_ids"] for e in db.scalars(select(AuditLog).where(
                AuditLog.action == "home_logo", AuditLog.console_id == cid).order_by(AuditLog.created_at))]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        cid = _console(client)
        asyncio.run(logos.restore(cid))
        assert mock.logos == {"PPSA01234": png, "PPSA05678": png} and sweeps() == [["PPSA05678"]]  # 1234's was right
        assert len(fetched) == 4  # each concept's logo once: kept on the PC
        asyncio.run(logos.restore(cid))
        reads.clear()
        asyncio.run(logos.restore(cid))
        assert not reads and sweeps() == [["PPSA05678"]]  # nothing changed: no logo file read, nothing written

        mock.logos.clear()  # a restart drops them all
        asyncio.run(logos.restore(cid))
        assert mock.logos == {"PPSA01234": png, "PPSA05678": png} and len(fetched) == 4

        logos.choose("PPSA01234", "custom", own)  # picked while the console was away
        logos.choose("PPSA05678", "none")
        asyncio.run(logos.restore(cid))
        assert mock.logos == {"PPSA01234": own}
        n = len(sweeps())
        asyncio.run(logos.restore(cid))
        assert len(sweeps()) == n  # nothing left to change

        logos.choose("PPSA05678", "store")  # back to the store's
        asyncio.run(logos.restore(cid))
        assert mock.logos == {"PPSA01234": own, "PPSA05678": png}


def test_home_logo_choice(tmp_path, monkeypatch):
    """A logo of the user's own, or none, by title ID; the push and new installs follow it. Editions sharing a concept
    share one logo: the first that has a pick decides."""
    monkeypatch.setattr(config, "COVERS_DIR", tmp_path / "covers")
    monkeypatch.setattr(logos, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(logos, "_get", lambda url: pytest.fail("a logo of the user's own doesn't ask the store"))
    mock = consoles.MockConsoleAdapter()
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: mock)
    out = io.BytesIO()
    Image.new("RGB", (5000, 500), "white").save(out, "JPEG")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        cid = _console(client)
        assert client.get("/api/v1/titles/PPSA05678/home-logo").json() == {"use": "store"}
        r = client.post("/api/v1/titles/PPSA05678/home-logo", files={"file": ("l.jpg", out.getvalue(), "image/jpeg")})
        assert r.json() == {"use": "custom"}
        assert client.post(f"/api/v1/consoles/{cid}/home-logo", json={"title_id": "PPSA05678"}).json()["message"] ==             "Logo is on the home screen."
        logo = Image.open(io.BytesIO(mock.logos["PPSA05678"]))
        assert logo.format == "PNG" and logo.mode == "RGBA" and logo.size == (2880, 288)  # shrunk to fit

        assert client.put("/api/v1/titles/PPSA05678/home-logo", json={"use": "none"}).json() == {"use": "none"}
        assert client.post(f"/api/v1/consoles/{cid}/home-logo", json={"title_id": "PPSA05678"}).json()["message"] ==             "Logo removed from the home screen."
        assert "PPSA05678" not in mock.logos
        assert client.put("/api/v1/titles/PPSA05678/home-logo", json={"use": "custom"}).status_code == 422  # upload it

    logos.choose("PPSA00002", "custom", b"two")
    assert logos.wanted(["PPSA00003", "PPSA00002", "PPSA00001"], lambda: b"store") == ("PPSA00002", "custom", b"two")
    logos.choose("PPSA00001", "none")
    assert logos.wanted(["PPSA00003", "PPSA00002", "PPSA00001"], lambda: b"store") == ("PPSA00001", "none", None)
    assert logos.wanted(["PPSA00009"], lambda: b"store") == ("PPSA00009", "store", b"store")


def test_logo_after_install(tmp_path, monkeypatch):
    png = logos.PNG + b"logo"
    monkeypatch.setattr(logos, "_get", lambda url: json.dumps(
        {"media": {"images": [{"type": "LOGO", "url": "https://image.api.playstation.com/l.png"}]}}).encode()
        if "/concepts/" in url else png)
    mock = consoles.MockConsoleAdapter()
    reads = 0

    async def home_db():  # ShadowMountPlus adds the new game to the home screen only after the first look
        nonlocal reads
        reads += 1
        if reads == 2:
            mock.icons.add("PPSA04444")
        return await consoles.MockConsoleAdapter.get_home_db(mock)
    mock.get_home_db = home_db
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: mock)
    monkeypatch.setattr(jobs, "LOGO_RETRY_SECONDS", 0)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        cid = _console(client)
        with SessionLocal() as db:
            g = Game(title="Racer", title_id="PPSA04444", format="image", source_path=str(tmp_path / "r.exfat"),
                     source_fingerprint="x", size_bytes=1, compatibility_status="ready")
            db.add(g)
            db.flush()
            job = InstallJob(console_id=cid, game_id=g.id, status="success", message="Installed on USB.")
            db.add(job)
            db.commit()
            asyncio.run(jobs._add_logo(job.id, cid, "PPSA04444"))
            db.refresh(job)
            assert job.message == "Installed on USB. Its logo is on the home screen." and reads == 2
            assert mock.logos == {"PPSA04444": png}

            monkeypatch.setattr(jobs, "LOGO_WAIT_SECONDS", 0)  # never shows up: gives up with a note
            asyncio.run(jobs._add_logo(job.id, cid, "PPSA09999"))
            db.refresh(job)
            assert "No home-screen logo:" in job.message


def test_store_poster_and_info(tmp_path, monkeypatch):
    def png(size, mode="RGB"):
        out = io.BytesIO()
        Image.new(mode, size).save(out, "PNG")
        return out.getvalue()
    art = {"PORTRAIT_BANNER": png((1440, 2160)), "GAMEHUB_COVER_ART": png((3840, 2160)), "LOGO": png((1200, 400), "RGBA")}
    concept = {"publisherName": "Pub", "releaseDate": {"date": "2023-01-24T05:00:00Z"},
               "localizedGenres": [{"value": "Role Playing Games"}], "contentRating": {"description": "PEGI 18"},
               "starRating": {"score": "3.45", "total": "19847"},
               "media": {"images": [{"type": t, "url": f"https://image.api.playstation.com/{t}"} for t in art]}}
    asked = []

    def get(url):
        asked.append(url)
        if "/titles/PPSA01111_00/" in url:
            return b"[]" if "country=US" in url else json.dumps([concept]).encode()  # sold in Europe only
        if "/titles/PPSA02222_00/" in url:  # no portrait art: a poster is made from the key art and logo
            return json.dumps([{"media": {"images": concept["media"]["images"][1:]}}]).encode()
        if "/titles/PPSA04444_00/" in url:  # only its Deluxe Edition has portrait art, and only in Japan
            return json.dumps([{"media": {"images": []}, "categorizedProducts": [{"ids": ["DELUXE"]}]}]).encode()
        if "/products/DELUXE" in url:
            images = concept["media"]["images"][:1] if "country=JP" in url else []
            return json.dumps({"media": {"images": images}}).encode()
        if "/titles/PPSA06666_00/" in url:  # a collection's concept, with the collection's own art...
            return json.dumps([concept]).encode()
        if "/products/UP0001-PPSA06666_00-OWNART0000000000" in url:  # ...but the game has art of its own
            return json.dumps({"media": {"images": concept["media"]["images"][:1]}}).encode()
        if "/titles/PPSA05555_00/" in url:  # no art at all
            return json.dumps([{"media": {"images": []}}]).encode()
        if "/titles/" in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return art[url.rsplit("/", 1)[1]]
    monkeypatch.setattr(logos, "_get", get)
    monkeypatch.setattr(logos, "STORE_DIR", tmp_path / "store")
    monkeypatch.setattr(config, "COVERS_DIR", tmp_path / "covers")
    (tmp_path / "covers").mkdir()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        with SessionLocal() as db:
            games = [Game(title=t, title_id=tid, format="image", source_path=str(tmp_path / f"{t}.exfat"),
                          source_fingerprint="x", size_bytes=1, compatibility_status="ready")
                     for t, tid in (("Euro", "PPSA01111"), ("Wide", "PPSA02222"), ("Deluxe", "PPSA04444"), ("Bare", "PPSA05555"),
                                   ("No ID", None))]
            db.add_all(games)
            db.commit()
            euro, wide, deluxe, bare, none = (g.id for g in games)
        r = client.get(f"/api/v1/games/{euro}/poster")
        assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (480, 720)  # shrunk to tile size
        assert [u.split("country=")[1][:2] for u in asked[:2]] == ["US", "GB"]
        assert client.get(f"/api/v1/games/{euro}/store").json() == {
            "publisher": "Pub", "release_date": "2023-01-24", "genres": ["Role Playing Games"], "rating": 3.5,
            "rating_count": 19847, "age_rating": "PEGI 18"}
        client.get(f"/api/v1/games/{euro}/poster")
        assert len(asked) == 5  # concept: US, GB; portrait: US, GB, the image; then cached
        assert Image.open(io.BytesIO(client.get(f"/api/v1/games/{wide}/poster").content)).size == (480, 720)
        asked.clear()
        assert client.get(f"/api/v1/games/{deluxe}/poster").status_code == 200
        assert "/products/DELUXE?country=JP" in asked[-2] and asked[-1].endswith("/PORTRAIT_BANNER")
        assert client.get(f"/api/v1/games/{bare}/poster").status_code == 404
        asked.clear()
        assert client.get(f"/api/v1/games/{bare}/poster").status_code == 404
        assert not asked  # no art: asked again only after a week
        assert logos.poster("PPSA06666", "UP0001-PPSA06666_00-OWNART0000000000")
        assert "/products/UP0001-PPSA06666_00-OWNART0000000000?country=US" in asked[1] and len(asked) == 3
        assert client.get(f"/api/v1/games/{none}/poster").status_code == 404
        assert client.get(f"/api/v1/games/{none}/store").json() is None
        (tmp_path / "covers" / f"{euro}.png").write_bytes(b"x")
        assert client.get(f"/api/v1/games/{euro}/poster").status_code == 404  # an uploaded cover wins

        asked.clear()
        assert logos.title_logo("PPSA01111").read_bytes() == art["LOGO"]  # the concept's logo, kept
        assert logos.title_logo("PPSA01111") and asked == ["https://image.api.playstation.com/LOGO"]
        assert logos.title_logo("PPSA05555") is None and (tmp_path / "store" / "PPSA05555-logo.none").is_file()

        assert logos.concept("PPSA03333") is None  # in no store
        monkeypatch.setattr(logos, "_get", lambda url: pytest.fail("a game the store lacks is asked about once a week"))
        assert logos.concept("PPSA03333") is None


def test_console_library_genres(tmp_path, monkeypatch):
    """Genres from the store, else the sidecar's; store pages not seen yet are looked up after answering."""
    monkeypatch.setattr(logos, "STORE_DIR", tmp_path / "store")
    looked_up = []
    monkeypatch.setattr(logos, "_get", lambda url: looked_up.append(url) or json.dumps(
        [{"localizedGenres": [{"value": "Action"}, {"value": "Role Playing Games"}]}]).encode())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        cid = _console(client)
        with SessionLocal() as db:
            store = Game(title="Store Game", title_id="PPSA07771", format="image", source_path=str(tmp_path / "s.exfat"),
                         source_fingerprint="x", size_bytes=1, compatibility_status="ready")
            sidecar = Game(title="Sidecar Game", format="image", genre="Puzzle", source_path=str(tmp_path / "p.exfat"),
                           source_fingerprint="x", size_bytes=1, compatibility_status="ready")
            db.add_all([store, sidecar])
            db.commit()
        auth = {"Authorization": f"Bearer {console_app_token(cid)}"}

        def rows():
            return {r.split("\t")[0]: r.split("\t") for r in client.get("/api/v1/console-app/library", headers=auth).text.splitlines()}
        asked = lambda: sum("PPSA07771" in url for url in looked_up)  # noqa: E731
        first = rows()  # not looked up yet; the lookup runs after this answer
        assert first[store.id][17] == "" and first[sidecar.id][17] == "Puzzle" and asked() >= 1
        assert first[store.id][7] == "0"  # no poster on this PC yet: the PS5 doesn't ask for a cover
        n = asked()
        assert rows()[store.id][17] == "Action|Role Playing Games" and asked() == n  # kept, not asked again

        # A poster that's on this PC counts as a cover, and is served without asking the store
        out = io.BytesIO()
        Image.new("RGB", (480, 720)).save(out, "JPEG")
        (tmp_path / "store" / "PPSA07771.jpg").write_bytes(out.getvalue())
        assert rows()[store.id][7] == "1"
        assert len(client.get(f"/api/v1/console-app/covers/{store.id}", headers=auth).content) == 256 * 320 * 4
        assert asked() == n

        # Likewise a logo: trimmed to its lettering and set bottom-left in the panel's title box
        assert rows()[store.id][18] == "0"
        assert client.get(f"/api/v1/console-app/logos/{store.id}", headers=auth).status_code == 404
        logo = Image.new("RGBA", (1000, 600))
        logo.paste((255, 255, 255, 255), (100, 200, 900, 400))  # 800x200 of lettering in transparent padding
        logo.save(tmp_path / "store" / "PPSA07771-logo.png")
        assert rows()[store.id][18] == "1" and rows()[sidecar.id][18] == "0"
        px = Image.frombytes("RGBA", (384, 112), client.get(f"/api/v1/console-app/logos/{store.id}", headers=auth).content)
        assert px.getbbox() == (0, 112 - 96, 384, 112)  # 800x200 scaled to 384 wide, on the bottom edge
        assert asked() == n


def test_ftp_console_app_upload(ps5):  # noqa: F811
    root, port, _ = ps5
    a = adapter(port)
    files = {"eboot.bin": b"elf", "sce_sys/param.json": b'{"titleId": "PPSA99787"}', "assets/version.txt": b"9.9.9"}
    asyncio.run(a.install_console_app(files))
    asyncio.run(a.install_console_app(files))  # an update over an existing copy
    assert (root / "data/homebrew/PPSA99787/sce_sys/param.json").exists()
    assert not list((root / "data/homebrew/PPSA99787").rglob("*.partial"))  # replaced in place, nothing left over
    status = asyncio.run(a.connect()).helper_status
    assert status["console_app"] == "9.9.9"
    assert consoles.CONSOLE_APP_TITLE_ID not in {g.title_id for g in asyncio.run(a.scan_installed_games())}


def test_relay_answers_the_ps5_app_through_the_helper(tmp_path, monkeypatch):
    """The PC takes a PS5 app request from the helper (GET /relay) and sends back its own API's answer (POST /relay)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from app import relay

    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        cid = _console(client)
        token = console_app_token(cid)
        answers, jobs_left = [], [{"id": 7, "method": "GET", "path": "/api/v1/console-app/library", "auth": f"Bearer {token}"}]

        class Helper(BaseHTTPRequestHandler):
            def do_GET(self):
                assert self.headers["X-Storefront-Key"] == consoles.helper_key()
                if jobs_left:
                    body = json.dumps(jobs_left.pop()).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(204)
                    self.end_headers()

            def do_POST(self):
                answers.append((self.path, self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        helper = ThreadingHTTPServer(("127.0.0.1", 0), Helper)
        threading.Thread(target=helper.serve_forever, daemon=True).start()
        # the PC's API is this test client here, not a server on config.PORT
        monkeypatch.setattr(relay, "_forward", lambda method, path, auth: (
            (r := client.request(method, path, headers={"Authorization": auth})).status_code, r.content))
        try:
            port = helper.server_address[1]
            assert relay.serve_one("127.0.0.1", consoles.helper_key(), port)
            assert not relay.serve_one("127.0.0.1", consoles.helper_key(), port)  # nothing waiting
        finally:
            helper.shutdown()
        path, body = answers[0]
        expected = client.get("/api/v1/console-app/library", headers={"Authorization": f"Bearer {token}"}).content
        assert path == "/relay?id=7&status=200" and body == expected


def test_relay_reaches_only_the_ps5_app_api():
    from app import relay
    assert relay._forward("GET", "/api/v1/games", "")[0] == 403  # refused before anything is asked


def test_relay_worker_survives_a_bad_request(monkeypatch):
    """A control character in a relayed path raises InvalidURL (not an OSError); the worker must keep relaying."""
    import http.client
    from app import relay
    calls = []

    def serve_one(host, key):
        calls.append(host)
        if len(calls) == 1:
            raise http.client.InvalidURL("URL can't contain control characters.")
        relay.forget("c1")  # second round: stop the loop
    monkeypatch.setattr(relay, "serve_one", serve_one)
    monkeypatch.setattr(relay, "RETRY_SECONDS", 0)
    monkeypatch.setattr(relay, "helper_key", lambda: "k")
    relay._wanted["c1"] = "10.0.0.5"
    relay._worker("c1", "10.0.0.5")
    assert len(calls) == 2


def test_refresh_store(tmp_path, monkeypatch):
    """Refresh all artwork forgets every store file and looks the library's games up again."""
    monkeypatch.setattr(logos, "STORE_DIR", tmp_path / "store")
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "PPSA07772.jpg").write_bytes(b"old")
    (tmp_path / "store" / "PPSA07772-logo.none").write_bytes(b"")
    warmed = []
    monkeypatch.setattr(main, "_warm_store", warmed.extend)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        with SessionLocal() as db:
            db.add(Game(title="Refresh Game", title_id="PPSA07772", content_id="UP0001-PPSA07772_00-REFRESH000000000",
                        format="image", source_path=str(tmp_path / "r.exfat"), source_fingerprint="x", size_bytes=1,
                        compatibility_status="ready"))
            db.commit()
        assert client.post("/api/v1/store/refresh").status_code == 202
    assert not (tmp_path / "store").exists()
    assert ("PPSA07772", "UP0001-PPSA07772_00-REFRESH000000000") in warmed


def test_title_cover_upload(tmp_path, monkeypatch):
    """A cover uploaded for a game only on a console wins over the store's poster."""
    monkeypatch.setattr(config, "COVERS_DIR", tmp_path / "covers")
    (tmp_path / "covers").mkdir()
    monkeypatch.setattr(logos, "poster", lambda *a: pytest.fail("an uploaded cover is served without asking the store"))
    out = io.BytesIO()
    Image.new("RGB", (40, 60), "red").save(out, "PNG")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        r = client.post("/api/v1/titles/PPSA07773/cover", files={"file": ("c.png", out.getvalue(), "image/png")})
        assert r.status_code == 204
        assert client.get("/api/v1/titles/PPSA07773/poster").content == out.getvalue()
        assert client.post("/api/v1/titles/PPSA07773/cover", files={"file": ("c.png", b"nope", "image/png")}).status_code == 415
        assert client.post("/api/v1/titles/nope/cover", files={"file": ("c.png", out.getvalue(), "image/png")}).status_code == 404


def test_upload_only_real_images(tmp_path, monkeypatch):
    """Whatever the browser claims, only JPEG, PNG and WebP decoders see an upload, and only up to MAX_IMAGE_PIXELS."""
    monkeypatch.setattr(config, "COVERS_DIR", tmp_path / "covers")

    def image(fmt, size=(40, 60), mode="RGB"):
        out = io.BytesIO()
        Image.new(mode, size).save(out, fmt)
        return out.getvalue()
    with TestClient(app, base_url="http://127.0.0.1") as client:
        login(client)
        up = lambda data, kind="image/png": client.post("/api/v1/titles/PPSA07774/home-logo",  # noqa: E731
                                                        files={"file": ("x.png", data, kind)}).status_code
        assert up(image("TIFF")) == 415 and up(image("BMP")) == 415 and up(image("GIF")) == 415  # disguised as PNG
        assert up(b"<svg onload=alert(1)>", "image/svg+xml") == 415
        assert up(image("PNG", (8000, 6000), "1")) == 413  # 48 megapixels in a few kB
        assert up(image("WEBP"), "image/jpeg") == 200  # a real image of an allowed kind, mislabelled, is fine
        r = client.post("/api/v1/titles/PPSA07775/cover", files={"file": ("c.jpg", image("PNG"), "image/jpeg")})
        assert r.status_code == 204 and (tmp_path / "covers" / "titles" / "PPSA07775.png").is_file()  # named by content


def test_logos_on_ftp_console(monkeypatch):
    """A console added over FTP runs the helper too, so its logos are kept up as well."""
    mock = consoles.MockConsoleAdapter()
    monkeypatch.setattr(inventory, "adapter_for", lambda db, c: mock)
    monkeypatch.setattr(logos, "cached_logo", lambda concept: logos.PNG + b"logo")
    with SessionLocal() as db:
        con = Console(name="FTP one", ip_address="127.0.0.2", connection_type="ftp", port=2121)
        db.add(con)
        db.commit()
        asyncio.run(logos.restore(con.id))
    assert set(mock.logos) == {"PPSA01234", "PPSA05678"}


def test_logo_sync_schedule(monkeypatch):
    """Logos are checked as a console comes online, then every SYNC_SECONDS while it stays online."""
    started = []
    monkeypatch.setattr(main.jobs, "_spawn", lambda coro: (started.append(1), coro.close()))
    clock = [1000.0]
    monkeypatch.setattr(inventory.time, "monotonic", lambda: clock[0])

    async def go():
        inventory.record("sched", "offline")
        inventory.record("sched", "online")  # back on
        inventory.record("sched", "online")  # 15 s later: too soon
        clock[0] += logos.SYNC_SECONDS
        inventory.record("sched", "online")  # due again
    asyncio.run(go())
    assert len(started) == 2
