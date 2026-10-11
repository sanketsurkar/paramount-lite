"""
app/security.py
---------------
Everything security-related that does NOT need the database:

* Password hashing   - PBKDF2-SHA256 from the standard library (hashlib)
* One-time passwords - 6 random characters for new users / admin resets
* Password policy    - 8+ characters, letters AND numbers
* Login rate limit   - max 5 failed attempts per login ID per minute
* CSRF tokens        - one random token per session, checked on every POST
* Session helpers    - what we store in the signed session cookie

.NET comparison: this is roughly PasswordHasher<T> + antiforgery + a small
rate limiter, written by hand so there are no extra packages.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import math
import secrets
import threading
import time
from collections import deque
from functools import lru_cache
from typing import Any

from starlette.requests import Request

from app.config import (
    LOGIN_MAX_FAILURES,
    LOGIN_WINDOW_SECONDS,
    MIN_PASSWORD_LENGTH,
    OTP_LENGTH,
    PBKDF2_ITERATIONS,
)

# ===========================================================================
# Password hashing
# Stored format (one string column):  pbkdf2_sha256$600000$<salt>$<hash>
# Storing the iteration count lets us raise it later without breaking
# existing passwords (see needs_rehash).
# ===========================================================================
_ALGORITHM = "pbkdf2_sha256"
_SALT_BYTES = 16
MAX_PASSWORD_LENGTH = 128   # stops someone posting a 10 MB "password"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return a salted hash string safe to store in users.password_hash."""
    salt = secrets.token_bytes(_SALT_BYTES)            # new random salt per user
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGORITHM}${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """True if `password` matches the stored hash. Never raises."""
    if len(password) > MAX_PASSWORD_LENGTH:
        return False
    try:
        algorithm, iterations_text, salt_b64, hash_b64 = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):      # malformed hash in the database
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    # compare_digest takes the same time whether the first or last byte differs,
    # so an attacker can't learn anything from response timing.
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """True if the hash used fewer iterations than today's setting."""
    try:
        return int(stored.split("$")[1]) < PBKDF2_ITERATIONS
    except (IndexError, ValueError):
        return True


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Created once, on first use (hashing is deliberately slow)."""
    return hash_password(secrets.token_urlsafe(16))


def burn_password_check(password: str) -> None:
    """
    Called when the login ID does not exist. It does the same slow work as a
    real check, so an attacker cannot tell valid IDs apart by response time.
    """
    verify_password(password, _dummy_hash())


# ===========================================================================
# One-time passwords and password policy
# ===========================================================================
# Lower-case letters + digits, without look-alikes (0/o, 1/l/i).
# Lower case is easier to type on phone keyboards.
_OTP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def generate_otp(length: int = OTP_LENGTH) -> str:
    """Random one-time password with at least one letter and one digit, e.g. 'k7m2xp'."""
    while True:
        otp = "".join(secrets.choice(_OTP_ALPHABET) for _ in range(length))
        if any(c.isalpha() for c in otp) and any(c.isdigit() for c in otp):
            return otp


def validate_new_password(password: str, login_id: str) -> list[str]:
    """Return a list of problems (an empty list means the password is acceptable)."""
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    if len(password) > MAX_PASSWORD_LENGTH:
        errors.append(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")
    if not any(c.isalpha() for c in password):
        errors.append("Password must contain at least one letter.")
    if not any(c.isdigit() for c in password):
        errors.append("Password must contain at least one number.")
    if password.strip().upper() == login_id.strip().upper():
        errors.append("Password cannot be the same as your login ID.")
    return errors


def normalise_login_id(raw: str) -> str:
    """' tpa26-0001 ' -> 'TPA26-0001' (IDs are stored upper-case)."""
    return raw.strip().upper()


# ===========================================================================
# Login rate limiter (in memory)
# We run a single server process, so a dictionary in memory is enough.
# It resets when the server restarts, which is acceptable for a
# per-minute limit.
# ===========================================================================
class LoginRateLimiter:
    """Allows at most `max_failures` failed logins per ID within `window_seconds`."""

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self._max = max_failures
        self._window = window_seconds
        self._failures: dict[str, deque[float]] = {}   # login_id -> failure times
        self._lock = threading.Lock()                   # like C# `lock (obj) { }`

    def _prune(self, key: str, now: float) -> deque[float] | None:
        """Drop failures older than the window; remove the key if nothing is left."""
        times = self._failures.get(key)
        if times is None:
            return None
        while times and now - times[0] >= self._window:
            times.popleft()
        if not times:
            del self._failures[key]
            return None
        return times

    def seconds_blocked(self, login_id: str) -> int:
        """0 if the ID may try to log in now, otherwise seconds left to wait."""
        key = normalise_login_id(login_id)
        now = time.monotonic()
        with self._lock:
            times = self._prune(key, now)
            if times is not None and len(times) >= self._max:
                return max(1, math.ceil(self._window - (now - times[0])))
        return 0

    def record_failure(self, login_id: str) -> None:
        key = normalise_login_id(login_id)
        now = time.monotonic()
        with self._lock:
            # Safety valve: if someone sprays thousands of fake IDs, start fresh.
            if len(self._failures) > 10_000:
                self._failures.clear()
            self._failures.setdefault(key, deque()).append(now)

    def reset(self, login_id: str) -> None:
        """Clear failures after a successful login."""
        with self._lock:
            self._failures.pop(normalise_login_id(login_id), None)


login_limiter = LoginRateLimiter(LOGIN_MAX_FAILURES, LOGIN_WINDOW_SECONDS)


# ===========================================================================
# CSRF protection
# Every session gets a random token. Each POST form includes it in a hidden
# field `csrf_token`, and JavaScript fetch calls send it in the X-CSRF-Token
# header. Another website can't read our pages, so it can't know the token.
# ===========================================================================
CSRF_SESSION_KEY = "csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"


def get_csrf_token(request: Request) -> str:
    """Return the session's CSRF token, creating one if needed."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[CSRF_SESSION_KEY] = token
    return token


def csrf_token_is_valid(request: Request, submitted: str | None) -> bool:
    expected = request.session.get(CSRF_SESSION_KEY)
    if not expected or not submitted:
        return False
    return hmac.compare_digest(str(expected), str(submitted))


# ===========================================================================
# Session helpers
# The session is a signed (not encrypted) cookie, so we store only small,
# non-secret values: the user's id and their session_version.
# ===========================================================================
SESSION_USER_KEY = "uid"
SESSION_VERSION_KEY = "sv"


def login_session(request: Request, user: Any) -> None:
    """Start a fresh session for `user` (a models.User)."""
    request.session.clear()   # new session on login = no session fixation
    request.session[SESSION_USER_KEY] = user.id
    request.session[SESSION_VERSION_KEY] = user.session_version
    request.session[CSRF_SESSION_KEY] = secrets.token_urlsafe(32)


def logout_session(request: Request) -> None:
    request.session.clear()


def safe_next_url(url: str | None, fallback: str = "/") -> str:
    """
    Only allow redirects to our own pages ('/student/results'), never to
    another site ('https://evil.example' or '//evil.example').
    """
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return fallback
