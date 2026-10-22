import os

from fastapi.testclient import TestClient

from app import config
from app.database import SessionLocal
from app import main
from app.main import app
from app.models import Game


def test_flow(library):
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/api/v1/auth/me").json()["error"] == "setup_required"
        assert c.post("/api/v1/auth/login", json={"password": "password1"}).status_code == 409
        assert c.post("/api/v1/auth/setup", json={"password": "short"}).status_code == 422
        r = c.post("/api/v1/auth/setup", json={"password": "password1"})
        assert r.status_code == 403 and "app.log" in r.json()["fix"]  # not from loopback: needs the code; says where it is
        assert c.post("/api/v1/auth/setup", json={"password": "password1", "code": "nope"}).json()["message"] == "Wrong setup code."
    # DNS rebinding: a website that points its own name at 127.0.0.1 must not reach setup (or anything else)
    with TestClient(app, base_url="http://evil.example:8787", client=("127.0.0.1", 50000)) as rebound:
        assert rebound.post("/api/v1/auth/setup", json={"password": "password1"}).status_code == 421
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as local:
        assert local.post("/api/v1/auth/setup", json={"password": "password1"}).status_code == 200
        assert local.get("/api/v1/auth/me").json() == {"user": "admin"}
        assert local.post("/api/v1/auth/setup", json={"password": "password2"}).status_code == 409
    with TestClient(app, base_url="http://127.0.0.1") as c:  # the right code gets past the check, to "already set up"
        code = main.SETUP_CODE.lower()
        assert c.post("/api/v1/auth/setup", json={"password": "password2", "code": code}).status_code == 409
    config.ALLOWED_HOSTS.add("tower.local")  # a NAS opened by its name
    try:
        with TestClient(app, base_url="http://tower.local:8787") as c:
            assert c.get("/api/v1/health").status_code == 200
    finally:
        config.ALLOWED_HOSTS.discard("tower.local")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/api/v1/games").json()["error"] == "authentication_required"
        assert c.post("/api/v1/auth/login", json={"password": "nope"}).status_code == 401
        assert c.post("/api/v1/auth/login", json={"password": "password1"}).status_code == 200
        assert c.get("http://[::1]:8787/api/v1/health").status_code == 200  # IPv6 literal with a port is fine

        saved = config.PASSWORD_FILE.read_text()  # a password reset logs existing sessions out
        config.PASSWORD_FILE.write_text(("1" if saved[0] != "1" else "2") + saved[1:])
        assert c.get("/api/v1/auth/me").status_code == 401
        config.PASSWORD_FILE.write_text(saved)
        assert c.get("/api/v1/auth/me").status_code == 200

        assert c.post("/api/v1/library/roots", json={"path": "relative/path"}).status_code == 400
        r = c.post("/api/v1/library/roots", json={"path": str(library / "not-there")})
        assert r.status_code == 400 and "no folder" in r.json()["message"] and "Browse" in r.json()["fix"]
        for r in c.get("/api/v1/library/roots").json():
            c.delete(f"/api/v1/library/roots/{r['id']}")
        r = c.post("/api/v1/library/roots", json={"path": str(library), "exclude_patterns": ["skip"]})
        assert r.status_code == 201, r.text

        assert c.post("/api/v1/library/scan").status_code == 202  # TestClient runs background tasks inline
        assert c.get("/api/v1/library/scan-status").json()["report"]["errors"] == []

        games = {g["title"]: g for g in c.get("/api/v1/games").json()}
        dump = games["Example Game"]
        assert dump["new"] and not c.get(f"/api/v1/games/{dump['id']}").json()["new"]  # opening it clears the mark
        assert not next(g for g in c.get("/api/v1/games").json() if g["id"] == dump["id"])["new"]
        assert dump["has_cover"] and c.get(f"/api/v1/games/{dump['id']}/cover").content == b"png"

        assert c.patch(f"/api/v1/games/{dump['id']}", json={"title_id": "bad"}).status_code == 422
        assert c.patch(f"/api/v1/games/{dump['id']}", json={"source_path": "/etc"}).status_code == 422
        copy = games["Copy"]
        c.delete(f"/api/v1/games/{copy['id']}")  # duplicate gone -> dump becomes ready
        g = c.patch(f"/api/v1/games/{dump['id']}", json={"favorite": True, "provenance_notes": "my disc"}).json()
        assert g["favorite"] and g["compatibility_status"] == "ready"

        other = games["Other"]  # title ID from filename only, until the user confirms it
        assert other["metadata_confidence"] == "low" and other["compatibility_status"] == "needs_attention"
        g = c.patch(f"/api/v1/games/{other['id']}", json={"title_id": other["title_id"]}).json()
        assert (g["metadata_source"], g["compatibility_status"]) == ("manual", "ready")

        r = c.post(f"/api/v1/games/{dump['id']}/cover", files={"file": ("c.png", b"newpng", "image/png")})
        assert r.status_code == 200 and c.get(f"/api/v1/games/{dump['id']}/cover").content == b"newpng"
        with SessionLocal() as db:  # data folder copied to another PC: the stored path is stale, the file moved along
            g = db.get(Game, dump["id"])
            g.cover_path = os.path.join("C:\\Users\\someone-else\\old", os.path.basename(g.cover_path))
            db.commit()
        assert c.get(f"/api/v1/games/{dump['id']}/cover").content == b"newpng"
        assert c.post(f"/api/v1/games/{dump['id']}/cover", files={"file": ("x.sh", b"#!", "text/x-sh")}).status_code == 415

        # Removing the root drops its catalog records too.
        for r in c.get("/api/v1/library/roots").json():
            c.delete(f"/api/v1/library/roots/{r['id']}")
        assert c.get("/api/v1/games").json() == []


def test_browse_and_drive_root_layout(tmp_path):
    from conftest import PARAM_JSON, write
    write(tmp_path / "Extracted" / "Game" / "sce_sys" / "param.json", PARAM_JSON.replace(b"PPSA02222", b"PPSA06666"))
    write(tmp_path / "Compressed" / "Game PPSA06666.7z", b"7z\xbc\xaf\x27\x1c" + b"\0" * 50)
    write(tmp_path / "$RECYCLE.BIN" / "Old PPSA08888.exfat", b"x")
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.post("/api/v1/auth/login", json={"password": "password1"})  # set up in test_flow
        drives = c.get("/api/v1/library/browse").json()
        assert drives["path"] == "" and drives["dirs"]
        listing = c.get("/api/v1/library/browse", params={"path": str(tmp_path)}).json()
        assert [os.path.basename(d) for d in listing["dirs"]] == ["Compressed", "Extracted"]
        assert listing["parent"] == os.path.dirname(os.path.realpath(tmp_path))
        assert c.get("/api/v1/library/browse", params={"path": str(tmp_path / "nope")}).status_code == 400

        for r in c.get("/api/v1/library/roots").json():
            c.delete(f"/api/v1/library/roots/{r['id']}")
        c.post("/api/v1/library/roots", json={"path": str(tmp_path)})
        c.post("/api/v1/library/scan")
        report = c.get("/api/v1/library/scan-status").json()["report"]
        assert not report["errors"] and not any("RECYCLE" in p for p in report["added"])
        dump = next(g for g in c.get("/api/v1/games").json() if g["format"] == "folder_dump" and g["title_id"] == "PPSA06666")
        assert "Duplicate" not in dump["compatibility_reason"]


def test_audit_log():
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.post("/api/v1/auth/login", json={"password": "nope"})
        c.post("/api/v1/auth/login", json={"password": "password1"})  # set up in test_flow
        rows = c.get("/api/v1/maintenance/audit-logs").json()
        assert [r["result"] for r in rows[:2]] == ["success", "failed"] and rows[0]["action"] == "login"
        older = c.get("/api/v1/maintenance/audit-logs", params={"before": rows[0]["created_at"]}).json()
        assert older[0]["id"] == rows[1]["id"]


def test_setup_checklist(tmp_path):
    """The checklist reads its steps from what's already set up, and its dismissal survives a reload."""
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.post("/api/v1/auth/login", json={"password": "password1"})  # set up in test_flow
        for r in c.get("/api/v1/library/roots").json():
            c.delete(f"/api/v1/library/roots/{r['id']}")
        s = c.get("/api/v1/setup").json()
        assert not s["folder"] and not s["dlc_folder"] and not s["dismissed"]
        c.post("/api/v1/library/roots", json={"path": str(tmp_path)})
        (tmp_path / "dlc").mkdir()
        c.post("/api/v1/library/roots", json={"path": str(tmp_path / "dlc"), "kind": "dlc"})
        s = c.get("/api/v1/setup").json()
        assert s["folder"] and s["dlc_folder"]
        assert c.post("/api/v1/setup", json={"dismissed": True}).json() == {"dismissed": True}
        assert c.get("/api/v1/setup").json()["dismissed"]
        c.post("/api/v1/setup", json={"dismissed": False})
        assert not c.get("/api/v1/setup").json()["dismissed"]
        for r in c.get("/api/v1/library/roots").json():
            c.delete(f"/api/v1/library/roots/{r['id']}")


def test_discover_marks_added_consoles(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "discover", lambda: [{"ip": "192.168.0.3", "name": "PS5-806", "state": "online"},
                                                   {"ip": "192.168.0.9", "name": "PS5-111", "state": "standby"}])
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.post("/api/v1/auth/login", json={"password": "password1"})  # set up in test_flow
        r = c.post("/api/v1/consoles", json={"name": "Den", "ip_address": "192.168.0.3", "connection_type": "mock", "port": 1})
        found = c.post("/api/v1/consoles/discover").json()
        assert [(p["name"], p["added"]) for p in found] == [("PS5-806", True), ("PS5-111", False)]
        c.delete(f"/api/v1/consoles/{r.json()['id']}")


def test_convert_folder_dump_to_exfat(tmp_path):
    import time
    from conftest import PARAM_JSON, write
    dump = tmp_path / "Native Game"
    write(dump / "sce_sys" / "param.json", PARAM_JSON)
    write(dump / "eboot.bin", b"elf" * 1000)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.post("/api/v1/auth/login", json={"password": "password1"})  # set up in test_flow
        for r in c.get("/api/v1/library/roots").json():
            c.delete(f"/api/v1/library/roots/{r['id']}")
        c.post("/api/v1/library/roots", json={"path": str(tmp_path)})
        c.post("/api/v1/library/scan")
        folder = c.get("/api/v1/games").json()[0]
        c.patch(f"/api/v1/games/{folder['id']}", json={"favorite": True})

        r = c.post(f"/api/v1/games/{folder['id']}/convert")
        assert r.status_code == 202 and r.json()["image_path"].endswith("Native Game [PPSA02222].exfat")
        for _ in range(100):
            if (conv := c.get(f"/api/v1/games/{folder['id']}/convert").json())["status"] != "converting":
                break
            time.sleep(0.1)
        assert conv["status"] == "success", conv
        image = c.get(f"/api/v1/games/{conv['image_game_id']}").json()
        assert (image["format"], image["image_subtype"], image["title_id"]) == ("image", "exfat", "PPSA02222")
        assert image["metadata_confidence"] == "high" and image["favorite"] and image["title"] == "Native Game"
        assert dump.is_dir()  # the folder is kept
        r = c.post(f"/api/v1/games/{folder['id']}/convert")
        assert r.status_code == 409 and "already exists" in r.json()["message"]
        assert c.post(f"/api/v1/games/{image['id']}/convert").status_code == 409  # only folder dumps


def test_update_check(monkeypatch):
    import io, json
    from app import __version__, main
    for tag, expect in [("v999.0.0", "999.0.0"), (f"v{__version__}", None)]:
        body = json.dumps({"tag_name": tag, "html_url": "https://example/r"}).encode()
        monkeypatch.setattr(main.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(body))
        monkeypatch.setattr(main, "_release_checked", None)
        with TestClient(app, base_url="http://127.0.0.1") as c:
            c.post("/api/v1/auth/login", json={"password": "password1"})  # set up in test_flow
            latest = c.get("/api/v1/update").json()["latest"]
        assert (latest and latest["version"]) == expect
    monkeypatch.setattr(main.urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(main, "_release_checked", None)
    assert main.latest_release() is None


def test_update_install(monkeypatch, tmp_path):
    import hashlib, io, json, threading, zipfile
    from app import main
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Spela/Spela.exe", "new")
        z.writestr("Spela/_internal/x.dll", "")
    files = {"/zip/Spela-999.0.0.zip": buf.getvalue()}
    files["/sums"] = f"{hashlib.sha256(files['/zip/Spela-999.0.0.zip']).hexdigest()} *Spela-999.0.0.zip\n".encode()
    release = json.dumps({"tag_name": "v999.0.0", "html_url": "https://example/r", "assets": [
        {"name": "Spela-999.0.0.zip", "browser_download_url": "https://x/zip/Spela-999.0.0.zip"},
        {"name": "SHA256SUMS.txt", "browser_download_url": "https://x/sums"}]}).encode()
    monkeypatch.setattr(main.urllib.request, "urlopen", lambda req, **k: io.BytesIO(
        release if isinstance(req, str) or "api.github" in req.full_url else files[req.full_url[len("https://x"):]]))
    monkeypatch.setattr(main, "_release_checked", None)
    monkeypatch.setattr(main.sys, "executable", str(tmp_path / "Spela" / "Spela.exe"))
    applied = threading.Event()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.post("/api/v1/auth/login", json={"password": "password1"})  # set up in test_flow
        assert c.get("/api/v1/update").json()["installable"] is False  # no launcher: dev or Linux
        assert c.post("/api/v1/update/install").status_code == 409
        monkeypatch.setattr(main, "apply_update", lambda new: (applied.set(), setattr(applied, "new", new)))
        assert c.get("/api/v1/update").json()["installable"] is True
        files["/sums"] = b"0000 *Spela-999.0.0.zip\n"
        r = c.post("/api/v1/update/install")
        assert r.status_code == 502 and "checksum" in r.json()["message"]
        assert not (tmp_path / "Spela.update-new").exists()
        files["/sums"] = f"{hashlib.sha256(files['/zip/Spela-999.0.0.zip']).hexdigest()} *Spela-999.0.0.zip\n".encode()
        assert c.post("/api/v1/update/install").json() == {"version": "999.0.0"}
        assert applied.wait(5) and applied.new == tmp_path / "Spela.update-new" / "Spela"
        assert (applied.new / "Spela.exe").read_text() == "new"
