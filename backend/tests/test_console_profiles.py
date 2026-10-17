from fastapi.testclient import TestClient
from sqlalchemy import select

from app import credentials
from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, Console, Secret

PW = "hunter2-very-secret"


def login(c):
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as local:
        local.post("/api/v1/auth/setup", json={"password": "password1"})  # 409 if another test did it first
    assert c.post("/api/v1/auth/login", json={"password": "password1"}).status_code == 200


def test_console_crud_with_encrypted_password():
    with TestClient(app, base_url="http://127.0.0.1") as c:
        assert c.get("/api/v1/consoles").status_code == 401
        login(c)
        base = {"name": "Living Room PS5", "ip_address": "192.168.1.50", "connection_type": "ftp", "port": 2121}
        assert c.post("/api/v1/consoles", json={**base, "ip_address": "not-an-ip"}).status_code == 422
        assert c.post("/api/v1/consoles", json={**base, "port": 70000}).status_code == 422
        assert c.post("/api/v1/consoles", json={**base, "connection_type": "telnet"}).status_code == 422

        r = c.post("/api/v1/consoles", json={**base, "username": "anon", "password": PW})
        assert r.status_code == 201, r.text
        con = r.json()
        assert con["has_password"] and "password" not in con and PW not in r.text

        with SessionLocal() as db:
            row = db.get(Console, con["id"])
            secret = db.get(Secret, row.credentials_secret_id)
            assert PW not in secret.ciphertext
            assert credentials.get_password(db, row) == PW

        # Patch without password keeps it; explicit null on a required field is rejected.
        r = c.patch(f"/api/v1/consoles/{con['id']}", json={"name": "Bedroom", "port": 1337})
        assert (r.json()["name"], r.json()["port"], r.json()["has_password"]) == ("Bedroom", 1337, True)
        assert c.patch(f"/api/v1/consoles/{con['id']}", json={"name": None}).status_code == 422
        assert c.patch(f"/api/v1/consoles/{con['id']}", json={"credentials_secret_id": "x"}).status_code == 422

        c.patch(f"/api/v1/consoles/{con['id']}", json={"password": "new-pass"})
        with SessionLocal() as db:
            assert credentials.get_password(db, db.get(Console, con["id"])) == "new-pass"
            assert db.scalar(select(Secret).where(Secret.id == secret.id))  # updated in place, not orphaned
        assert c.patch(f"/api/v1/consoles/{con['id']}", json={"password": ""}).json()["has_password"] is False

        assert PW not in c.get("/api/v1/consoles").text
        assert c.delete(f"/api/v1/consoles/{con['id']}").json() == {"ok": True}
        assert c.delete(f"/api/v1/consoles/{con['id']}").status_code == 404
        assert c.get("/api/v1/consoles").json() == []

        with SessionLocal() as db:
            assert db.scalars(select(Secret)).all() == []
            logs = db.scalars(select(AuditLog).where(AuditLog.console_id == con["id"])).all()
            assert [l.action for l in logs].count("credential_update") == 3
            assert not any(PW in str(l.details) for l in logs)


def test_view_only_login():
    with TestClient(app, base_url="http://127.0.0.1") as admin, TestClient(app, base_url="http://127.0.0.1") as viewer:
        login(admin)
        assert admin.post("/api/v1/auth/viewer", json={"password": "password1"}).json()["error"] == "same_as_admin"
        assert admin.post("/api/v1/auth/viewer", json={"password": "lookonly1"}).json() == {"enabled": True}

        assert viewer.post("/api/v1/auth/login", json={"password": "lookonly1"}).json() == {"user": "viewer"}
        assert viewer.get("/api/v1/auth/me").json() == {"user": "viewer"}
        assert viewer.get("/api/v1/games").status_code == 200
        for path in ("/maintenance/audit-logs", "/library/browse", "/auth/viewer"):  # admin-only, even to read
            assert viewer.get(f"/api/v1{path}").json()["error"] == "admin_only"
        r = viewer.post("/api/v1/library/scan")
        assert (r.status_code, r.json()["error"]) == (403, "read_only")
        assert viewer.post("/api/v1/auth/viewer", json={"password": None}).status_code == 403  # can't turn itself on or off
        assert admin.get("/api/v1/games").status_code == 200  # the admin session is untouched

        assert admin.post("/api/v1/auth/viewer", json={"password": None}).json() == {"enabled": False}
        assert viewer.get("/api/v1/games").status_code == 401  # logged out with it
        assert viewer.post("/api/v1/auth/login", json={"password": "lookonly1"}).status_code == 401


def test_wake_key_and_wake(monkeypatch):
    import socket

    from app import consoles, main
    from app.models import AuditLog
    with TestClient(app, base_url="http://127.0.0.1") as c:
        login(c)
        con = c.post("/api/v1/consoles", json={"name": "Sleepy", "ip_address": "127.0.0.1", "connection_type": "ftp",
                                               "port": 2121, "wake_key": "1a2b3c4d"}).json()
        assert con["has_wake_key"] and "1a2b3c4d" not in str(con)
        assert c.patch(f"/api/v1/consoles/{con['id']}", json={"wake_key": "not a key"}).json()["error"] == "invalid_wake_key"
        with SessionLocal() as db:
            assert credentials.get_password(db, db.get(Console, con["id"]), "wake_secret_id") == "439041101"

        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        listener.settimeout(5)
        monkeypatch.setattr(consoles, "DDP_PORT", listener.getsockname()[1])
        monkeypatch.setattr(main, "WAKE_WAIT_SECONDS", 0)
        assert c.post(f"/api/v1/consoles/{con['id']}/wake").status_code == 200
        assert listener.recv(1024) == (b"WAKEUP * HTTP/1.1\nclient-type:vr\nauth-type:R\nmodel:w\napp-type:r\n"
                                       b"user-credential:439041101\ndevice-discovery-protocol-version:00030010\n")
        listener.close()

        assert c.patch(f"/api/v1/consoles/{con['id']}", json={"wake_key": ""}).json()["has_wake_key"] is False
        assert c.post(f"/api/v1/consoles/{con['id']}/wake").json()["error"] == "no_wake_key"
        with SessionLocal() as db:
            assert db.scalar(select(AuditLog).where(AuditLog.console_id == con["id"], AuditLog.action == "wake"))
        assert c.delete(f"/api/v1/consoles/{con['id']}").status_code == 200
