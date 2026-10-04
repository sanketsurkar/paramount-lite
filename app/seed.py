"""
app/seed.py
-----------
First-run data: settings, the admin account and default subjects.
Every function is safe to run again - it only adds what is missing.

Command line:
    python -m app.seed                 create tables + defaults
    python -m app.seed --reset-admin   give ADM-001 a new one-time password
                                       (recovery if the admin is locked out)

main.py also calls seed_database() automatically on startup.
"""
from __future__ import annotations

import argparse
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import (
    ADMIN_LOGIN_ID,
    DEFAULT_SETTINGS,
    DEFAULT_SUBJECTS,
    ROLE_ADMIN,
    STANDARDS,
    current_academic_year,
    settings,
    utcnow,
)
from app.db import SessionLocal, init_db
from app.models import AuditLog, Setting, Subject, User
from app.security import generate_otp, hash_password

CURRENT_YEAR_KEY = "academic_year.current"
# Remembers that default subjects were created once, so if the admin later
# removes them they don't come back on the next restart.
SUBJECTS_SEEDED_KEY = "seed.default_subjects_done"


def _audit(db: Session, action: str, entity: str, entity_id: str | None, details: dict) -> None:
    """System-generated audit entry (no logged-in user)."""
    db.add(
        AuditLog(
            user_id=None,
            actor_login_id="SYSTEM",
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=json.dumps(details, ensure_ascii=False),
        )
    )


def seed_settings(db: Session) -> int:
    """Insert missing settings keys. Returns how many were added."""
    wanted = {**DEFAULT_SETTINGS, CURRENT_YEAR_KEY: current_academic_year()}
    existing = set(db.scalars(select(Setting.key)))
    added = 0
    for key, value in wanted.items():
        if key not in existing:
            db.add(Setting(key=key, value=value))
            added += 1
    return added


def seed_admin(db: Session) -> str | None:
    """Create ADM-001 if missing. Returns its one-time password, or None if it existed."""
    exists = db.scalar(select(User.id).where(User.login_id == ADMIN_LOGIN_ID))
    if exists:
        return None
    otp = generate_otp()
    db.add(
        User(
            login_id=ADMIN_LOGIN_ID,
            full_name="Academy Administrator",
            role=ROLE_ADMIN,
            password_hash=hash_password(otp),
            must_change_password=True,
        )
    )
    _audit(db, "user.create", "user", ADMIN_LOGIN_ID, {"role": ROLE_ADMIN, "by": "seed"})
    return otp


def seed_default_subjects(db: Session) -> int:
    """English, Mathematics, Science for every class that has no subjects yet (first run only)."""
    if db.get(Setting, SUBJECTS_SEEDED_KEY) is not None:
        return 0
    created = 0
    for standard, _label in STANDARDS:
        count = db.scalar(
            select(func.count()).select_from(Subject).where(Subject.standard == standard)
        )
        if count:
            continue
        for position, (name, colour, icon) in enumerate(DEFAULT_SUBJECTS):
            db.add(Subject(standard=standard, name=name, colour=colour, icon=icon, position=position))
            created += 1
    db.add(Setting(key=SUBJECTS_SEEDED_KEY, value=utcnow().isoformat(timespec="seconds")))
    return created


def seed_database() -> dict:
    """Run every seed step in one transaction and return a summary."""
    with SessionLocal() as db:            # `with` = C# `using`
        summary = {
            "settings_added": seed_settings(db),
            "subjects_added": seed_default_subjects(db),
            "admin_otp": seed_admin(db),
        }
        db.commit()
    return summary


def reset_admin_password() -> str:
    """Emergency reset for ADM-001: new one-time password, all sessions logged out."""
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.login_id == ADMIN_LOGIN_ID))
        if admin is None:
            raise SystemExit("Admin account not found - run `python -m app.seed` first.")
        otp = generate_otp()
        admin.password_hash = hash_password(otp)
        admin.must_change_password = True
        admin.is_active = True
        admin.session_version += 1        # invalidates existing logins
        _audit(db, "password.reset", "user", ADMIN_LOGIN_ID, {"by": "command line"})
        db.commit()
    return otp


def print_admin_otp(otp: str, heading: str) -> None:
    """Print the one-time password in an easy-to-spot box (also shows in host logs)."""
    print("=" * 52)
    print(f"  {heading}")
    print(f"  Login ID          : {ADMIN_LOGIN_ID}")
    print(f"  One-time password : {otp}")
    print("  You will be asked to choose a new password at login.")
    print("=" * 52)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed The Paramount Academy database.")
    parser.add_argument("--reset-admin", action="store_true", help="give ADM-001 a new one-time password")
    args = parser.parse_args()

    init_db()
    print(f"Database ready: {settings.database_url.split('@')[-1]}")   # hides any password

    if args.reset_admin:
        print_admin_otp(reset_admin_password(), "Admin password reset")
        return

    summary = seed_database()
    print(f"Settings added: {summary['settings_added']}")
    print(f"Default subjects added: {summary['subjects_added']}")
    if summary["admin_otp"]:
        print_admin_otp(summary["admin_otp"], "Admin account created")
    else:
        print("Admin account already exists (use --reset-admin if you are locked out).")


# Runs only for `python -m app.seed`, not when another module imports this file
# (like a Main() method that is only the entry point when started directly).
if __name__ == "__main__":
    main()
