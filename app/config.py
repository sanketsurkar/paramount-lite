"""
app/config.py
-------------
Central configuration. Think of this as appsettings.json + a strongly typed
options class in .NET: every setting is read ONCE from environment variables
(or an optional .env file in the project root) and exposed through `settings`.

It also holds app-wide constants (class list, default subjects, ID formats,
password rules) and small date/time helpers, so there is one source of truth.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths  (pathlib.Path is like System.IO.Path, but object-based: dir / "file")
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent   # ...\paramount-lite
APP_DIR = BASE_DIR / "app"
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
INSTANCE_DIR = BASE_DIR / "instance"                 # local DB + uploads (never commit)
UPLOAD_DIR = INSTANCE_DIR / "uploads"


# ---------------------------------------------------------------------------
# Tiny .env loader (we avoid an extra package like python-dotenv).
# Lines look like KEY=VALUE; '#' starts a comment. Real environment
# variables always win over the file (setdefault does not overwrite).
# ---------------------------------------------------------------------------
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    """Read an environment variable as a trimmed string."""
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    """Read an environment variable as int, falling back to default."""
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _normalise_db_url(url: str) -> str:
    """
    Hosts like Neon/Render give 'postgres://...' or 'postgresql://...'.
    SQLAlchemy must be told to use the psycopg 3 driver: 'postgresql+psycopg://'.
    """
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


# ---------------------------------------------------------------------------
# Settings object (frozen dataclass = immutable record class in C#)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    app_name: str
    env: str                    # "development" or "production"
    secret_key: str             # signs the session cookie + CSRF tokens
    database_url: str
    session_max_age: int        # seconds a login stays valid
    max_upload_bytes: int
    supabase_url: str
    supabase_key: str
    supabase_bucket: str

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def use_supabase(self) -> bool:
        """Cloud file storage is used only when both values are configured."""
        return bool(self.supabase_url and self.supabase_key)


def load_settings() -> Settings:
    env = _env("APP_ENV", "development").lower()
    default_db = "sqlite:///" + (INSTANCE_DIR / "paramount.db").as_posix()

    s = Settings(
        app_name=_env("APP_NAME", "The Paramount Academy"),
        env=env,
        secret_key=_env("SECRET_KEY", "dev-only-insecure-key-change-me"),
        database_url=_normalise_db_url(_env("DATABASE_URL", default_db)),
        # Students stay logged in on their phones for 30 days by default.
        session_max_age=_env_int("SESSION_MAX_AGE_DAYS", 30) * 24 * 3600,
        max_upload_bytes=_env_int("MAX_UPLOAD_MB", 10) * 1024 * 1024,
        supabase_url=_env("SUPABASE_URL").rstrip("/"),
        supabase_key=_env("SUPABASE_SERVICE_KEY"),
        supabase_bucket=_env("SUPABASE_BUCKET", "paramount-files"),
    )

    # Fail fast in production instead of running with unsafe settings.
    if s.is_production:
        problems: list[str] = []
        if s.secret_key.startswith("dev-only") or len(s.secret_key) < 32:
            problems.append("SECRET_KEY must be a random string of 32+ characters")
        if s.is_sqlite:
            problems.append("DATABASE_URL must point to PostgreSQL")
        if not s.use_supabase:
            problems.append(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY are required "
                "(free hosts wipe their disk on restart, so uploads would be lost)"
            )
        if problems:
            raise RuntimeError("Production configuration error:\n - " + "\n - ".join(problems))
    return s


# ---------------------------------------------------------------------------
# Roles and ID formats
# ---------------------------------------------------------------------------
ROLE_ADMIN = "admin"
ROLE_TEACHER = "teacher"
ROLE_STUDENT = "student"          # parents use the student's login
ROLES = (ROLE_ADMIN, ROLE_TEACHER, ROLE_STUDENT)

ADMIN_LOGIN_ID = "ADM-001"        # single admin
TEACHER_ID_PREFIX = "TCH-"        # TCH-001, TCH-002 ...
STUDENT_ID_PREFIX = "TPA"         # TPA26-0001 (26 = admission year)

# ---------------------------------------------------------------------------
# Classes ("standards"). Stored as integers so Promote is simply +1:
#   Nursery=-2, LKG=-1, UKG=0, Class 1..12 = 1..12
# ---------------------------------------------------------------------------
STANDARDS: list[tuple[int, str]] = (
    [(-2, "Nursery"), (-1, "LKG"), (0, "UKG")]
    + [(n, f"Class {n}") for n in range(1, 13)]
)
STANDARD_LABELS: dict[int, str] = dict(STANDARDS)
MIN_STANDARD, MAX_STANDARD = -2, 12

STREAM_STANDARDS = (11, 12)                       # only these classes have streams
STREAMS = ("Science", "Commerce", "Arts")


def standard_label(code: int | None) -> str:
    """-2 -> 'Nursery', 7 -> 'Class 7', None -> 'Everyone' (used by notices)."""
    if code is None:
        return "Everyone"
    return STANDARD_LABELS.get(code, f"Class {code}")


# Default subjects for every class: (name, colour, emoji icon)
DEFAULT_SUBJECTS: tuple[tuple[str, str, str], ...] = (
    ("English", "#2563eb", "📘"),
    ("Mathematics", "#f59e0b", "➗"),
    ("Science", "#16a34a", "🔬"),
)

# ---------------------------------------------------------------------------
# Security rules
# ---------------------------------------------------------------------------
PBKDF2_ITERATIONS = 600_000       # OWASP recommendation for PBKDF2-SHA256
OTP_LENGTH = 6                    # one-time password for new users / resets
MIN_PASSWORD_LENGTH = 8           # must contain letters AND numbers
LOGIN_MAX_FAILURES = 5            # per login ID ...
LOGIN_WINDOW_SECONDS = 60         # ... per minute

# ---------------------------------------------------------------------------
# Files and fees
# ---------------------------------------------------------------------------
# Allowed upload types (checked against the file's real signature later).
ALLOWED_UPLOADS: dict[str, str] = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
PAYMENT_METHODS = ("UPI", "Cash", "Bank transfer")
RECEIPT_PREFIX = "RCPT"           # RCPT-2026-0001

# Values copied into the `settings` table on first run (admin can edit later).
# These are DUMMY details - replace before going live.
DEFAULT_SETTINGS: dict[str, str] = {
    "academy.name": "The Paramount Academy",
    "academy.tagline": "Excellence in every step",
    "academy.address": "12 Knowledge Park, Sector 5, New Delhi 110001",
    "academy.phone": "+91 98765 43210",
    "academy.email": "info@paramountacademy.example",
    "upi.id": "paramountacademy@upi",
    "upi.payee": "The Paramount Academy",
}

# ---------------------------------------------------------------------------
# Date/time helpers. India has no daylight saving, so a fixed +05:30 offset
# is exact (and avoids zoneinfo, which needs an extra package on Windows).
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def utcnow() -> datetime:
    """
    Current UTC time WITHOUT tzinfo ("naive"). All timestamps are stored as
    naive UTC, which behaves identically on SQLite and PostgreSQL.
    Convert with to_ist() only when displaying.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_ist(dt: datetime | None) -> datetime | None:
    """Convert a stored naive-UTC datetime to Indian time for display."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(IST)


def today_ist() -> date:
    """Today's date in India - used for due dates, notice expiry, etc."""
    return datetime.now(IST).date()


def academic_year_for(d: date) -> str:
    """April-March years: 2026-10-04 -> '2026-27', 2027-02-01 -> '2026-27'."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def current_academic_year() -> str:
    return academic_year_for(today_ist())


# The single settings instance every module imports:
#   from app.config import settings
settings = load_settings()
