"""
tests/smoke_test.py
-------------------
End-to-end check of Phase 1 (login, forced password change, dashboard,
CSRF, rate limit, logout). Uses a TEMPORARY database, so your real
instance/paramount.db is never touched.

    python tests/smoke_test.py

Every line should start with PASS and the last line should be ALL GOOD.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

# Point the app at a throw-away SQLite file BEFORE importing it.
_tmp = Path(tempfile.mkdtemp()) / "smoke.db"
os.environ["DATABASE_URL"] = "sqlite:///" + _tmp.as_posix()
os.environ["APP_ENV"] = "development"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditLog, User  # noqa: E402
from app.security import hash_password  # noqa: E402


def csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def ok(condition: bool, message: str) -> None:
    print(("PASS " if condition else "FAIL ") + message)
    if not condition:
        sys.exit(1)


with TestClient(app) as c:
    # set a known OTP for the admin (startup seed generated a random one)
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login_id=="ADM-001"))
        u.password_hash = hash_password("abc234"); db.commit()

    r = c.get("/", follow_redirects=False); ok(r.status_code==303 and r.headers["location"]=="/login", "root -> /login")
    r = c.get("/login"); ok(r.status_code==200 and "Log in" in r.text, "login page renders")
    ok("script-src 'self'" in r.headers.get("content-security-policy",""), "CSP header on HTML")
    tok = csrf(r.text)

    r = c.post("/login", data={"login_id":"adm-001","password":"x","csrf_token":"bad"}, follow_redirects=False)
    ok(r.status_code==403, "bad CSRF rejected")

    r = c.post("/login", data={"login_id":"adm-001","password":"wrong1","csrf_token":tok})
    ok(r.status_code==400 and "Incorrect login ID or password" in r.text, "wrong password message")

    r = c.post("/login", data={"login_id":" adm-001 ","password":"abc234","csrf_token":tok}, follow_redirects=False)
    ok(r.status_code==303 and r.headers["location"]=="/change-password", "OTP login -> forced change")

    r = c.get("/admin", follow_redirects=False); ok(r.headers.get("location")=="/change-password", "dashboard blocked until change")
    r = c.get("/change-password"); ok("Set your own password" in r.text, "change page renders"); tok = csrf(r.text)

    r = c.post("/change-password", data={"current_password":"abc234","new_password":"short","confirm_password":"short","csrf_token":tok})
    ok(r.status_code==400 and "at least 8" in r.text, "weak password rejected")

    r = c.post("/change-password", data={"current_password":"abc234","new_password":"Paramount2026","confirm_password":"Paramount2026","csrf_token":tok}, follow_redirects=False)
    ok(r.status_code==303 and r.headers["location"]=="/admin", "password changed -> /admin")

    r = c.get("/admin"); ok(r.status_code==200 and "the academy today" in r.text, "dashboard renders")
    ok("Password updated successfully" in r.text, "toast shown")
    ok('data-count="0"' in r.text and "Set up the academy" in r.text, "stats + setup panel")
    tok = csrf(r.text)

    r = c.get("/admin/nope"); ok(r.status_code==404 and "Page not found" in r.text, "styled 404")
    r = c.get("/static/app.css?v=1"); ok(r.status_code==200 and "immutable" in r.headers.get("cache-control",""), "static cached")
    r = c.get("/healthz"); ok(r.json()=={"status":"ok"}, "healthz")

    r = c.post("/logout", data={"csrf_token":tok}, follow_redirects=False)
    ok(r.headers["location"]=="/login?logged_out=1", "logout")
    r = c.get("/admin", follow_redirects=False); ok(r.headers["location"].startswith("/login?next=%2Fadmin"), "protected after logout")
    r = c.get("/login?logged_out=1"); ok("You have been logged out" in r.text, "logged out message"); tok=csrf(r.text)

    r = c.post("/login", data={"login_id":"ADM-001","password":"Paramount2026","csrf_token":tok,"next":"/admin"}, follow_redirects=False)
    ok(r.headers["location"]=="/admin", "login with new password + next")

    # rate limit
    r = c.post("/logout", data={"csrf_token":csrf(c.get('/admin').text)}, follow_redirects=False)
    tok = csrf(c.get("/login").text)
    for i in range(5): c.post("/login", data={"login_id":"TPA26-0099","password":"nope","csrf_token":tok})
    r = c.post("/login", data={"login_id":"TPA26-0099","password":"nope","csrf_token":tok})
    ok(r.status_code==429 and "Too many failed attempts" in r.text, "rate limit after 5 failures")

    r = c.get("/admin", headers={"accept":"application/json"}, follow_redirects=False)
    ok(r.status_code==401 and r.json()["error"]=="login_required", "JSON 401 for fetch")

    with SessionLocal() as db:
        ok(db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=="password.change"))==1, "audit logged")
print("ALL GOOD")
