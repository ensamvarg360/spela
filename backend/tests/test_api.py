import os

from fastapi.testclient import TestClient

from app.main import app


def test_flow(library):
    with TestClient(app) as c:
        assert c.get("/api/v1/auth/me").json()["error"] == "setup_required"
        assert c.post("/api/v1/auth/login", json={"password": "password1"}).status_code == 409
        assert c.post("/api/v1/auth/setup", json={"password": "short"}).status_code == 422
        assert c.post("/api/v1/auth/setup", json={"password": "password1"}).status_code == 403  # not from loopback
    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        assert local.post("/api/v1/auth/setup", json={"password": "password1"}).status_code == 200
        assert local.get("/api/v1/auth/me").json() == {"user": "admin"}
        assert local.post("/api/v1/auth/setup", json={"password": "password2"}).status_code == 409
    with TestClient(app) as c:
        assert c.get("/api/v1/games").json()["error"] == "authentication_required"
        assert c.post("/api/v1/auth/login", json={"password": "nope"}).status_code == 401
        assert c.post("/api/v1/auth/login", json={"password": "password1"}).status_code == 200

        assert c.post("/api/v1/library/roots", json={"path": "relative/path"}).status_code == 400
        for r in c.get("/api/v1/library/roots").json():
            c.delete(f"/api/v1/library/roots/{r['id']}")
        r = c.post("/api/v1/library/roots", json={"path": str(library), "exclude_patterns": ["skip"]})
        assert r.status_code == 201, r.text

        assert c.post("/api/v1/library/scan").status_code == 202  # TestClient runs background tasks inline
        assert c.get("/api/v1/library/scan-status").json()["report"]["errors"] == []

        games = {g["title"]: g for g in c.get("/api/v1/games").json()}
        dump = games["Example Game"]
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
    with TestClient(app) as c:
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
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"password": "nope"})
        c.post("/api/v1/auth/login", json={"password": "password1"})  # set up in test_flow
        rows = c.get("/api/v1/maintenance/audit-logs").json()
        assert [r["result"] for r in rows[:2]] == ["success", "failed"] and rows[0]["action"] == "login"
        older = c.get("/api/v1/maintenance/audit-logs", params={"before": rows[0]["created_at"]}).json()
        assert older[0]["id"] == rows[1]["id"]
