from fastapi.testclient import TestClient
from sqlalchemy import select

from app import credentials
from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, Console, Secret

PW = "hunter2-very-secret"


def login(c):
    with TestClient(app, client=("127.0.0.1", 50000)) as local:
        local.post("/api/v1/auth/setup", json={"password": "password1"})  # 409 if another test did it first
    assert c.post("/api/v1/auth/login", json={"password": "password1"}).status_code == 200


def test_console_crud_with_encrypted_password():
    with TestClient(app) as c:
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
