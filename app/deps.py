"""
app/deps.py
-----------
FastAPI "dependencies" - small functions FastAPI calls before an endpoint
and whose results it passes in as parameters. They play the role that
[Authorize(Roles = "...")], filters and constructor injection play in ASP.NET.

Usage in a route:

    @router.get("/admin")
    def dashboard(user: AdminUser, db: DbSession): ...

`AdminUser` means: "log in required, role must be admin, give me the User".
"""
from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import ROLE_ADMIN, ROLE_STUDENT, ROLE_TEACHER
from app.db import get_db
from app.models import Student, User
from app.security import (
    CSRF_FORM_FIELD,
    CSRF_HEADER,
    SESSION_USER_KEY,
    SESSION_VERSION_KEY,
    csrf_token_is_valid,
)

# A database session per request.  `db: DbSession` in any endpoint.
DbSession = Annotated[Session, Depends(get_db)]

LOGIN_PATH = "/login"
CHANGE_PASSWORD_PATH = "/change-password"
# Pages a user who must change their password is still allowed to open.
ALLOWED_DURING_PASSWORD_CHANGE = {CHANGE_PASSWORD_PATH, "/logout"}


class RedirectException(Exception):
    """
    Raised by a dependency to send the browser elsewhere (dependencies can't
    return a response). main.py registers a handler that turns it into a
    303 redirect - or a 401 JSON reply for JavaScript fetch calls.
    """

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


def home_url_for(user: User) -> str:
    """The landing page for each role."""
    if user.role == ROLE_ADMIN:
        return "/admin"
    if user.role == ROLE_TEACHER:
        return "/staff"
    return "/student"


# ---------------------------------------------------------------------------
# Who is logged in?
# ---------------------------------------------------------------------------
def get_optional_user(request: Request, db: DbSession) -> User | None:
    """
    Return the logged-in User, or None. A session is rejected (and cleared)
    if the user was deactivated or their password changed since login
    (session_version no longer matches).
    """
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        return None
    user = db.get(User, user_id)          # like DbSet.Find(id)
    if (
        user is None
        or not user.is_active
        or request.session.get(SESSION_VERSION_KEY) != user.session_version
    ):
        request.session.clear()
        return None
    request.state.user = user             # handy for templates and logging
    return user


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def require_user(request: Request, user: OptionalUser) -> User:
    """Any logged-in user. Also enforces the 'change your one-time password' rule."""
    if user is None:
        target = LOGIN_PATH
        # Remember where they were going (GET only - we can't replay a POST).
        if request.method == "GET":
            next_url = request.url.path
            if request.url.query:
                next_url += "?" + request.url.query
            target += "?next=" + quote(next_url, safe="")
        raise RedirectException(target)

    if user.must_change_password and request.url.path not in ALLOWED_DURING_PASSWORD_CHANGE:
        raise RedirectException(CHANGE_PASSWORD_PATH)
    return user


CurrentUser = Annotated[User, Depends(require_user)]


# ---------------------------------------------------------------------------
# Role checks  (like [Authorize(Roles = "Admin")])
# ---------------------------------------------------------------------------
def _require_roles(*roles: str):
    """Build a dependency that allows only the given roles."""

    def checker(user: CurrentUser) -> User:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You don't have access to this page.")
        return user

    return checker


require_admin = _require_roles(ROLE_ADMIN)
require_staff = _require_roles(ROLE_ADMIN, ROLE_TEACHER)
require_student = _require_roles(ROLE_STUDENT)

AdminUser = Annotated[User, Depends(require_admin)]      # admin only (fees, settings)
StaffUser = Annotated[User, Depends(require_staff)]      # admin or teacher
StudentUser = Annotated[User, Depends(require_student)]  # student / parent


def get_current_student(user: StudentUser) -> Student:
    """The Student row for the logged-in student."""
    if user.student is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Student profile not found. Please contact the academy."
        )
    return user.student


CurrentStudent = Annotated[Student, Depends(get_current_student)]


# ---------------------------------------------------------------------------
# Class-level access for teachers
# ---------------------------------------------------------------------------
def allowed_standards(user: User) -> list[int] | None:
    """
    Classes this user may work with.
    None  -> every class (admin)
    [..]  -> only these classes (teacher); empty list for anyone else
    """
    if user.role == ROLE_ADMIN:
        return None
    if user.role == ROLE_TEACHER and user.teacher is not None:
        return user.teacher.standard_codes
    return []


def ensure_standard_access(user: User, standard: int) -> None:
    """Raise 403 if a teacher tries to open a class they aren't assigned to."""
    allowed = allowed_standards(user)
    if allowed is not None and standard not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are not assigned to this class.")


# ---------------------------------------------------------------------------
# CSRF check for every state-changing request
# ---------------------------------------------------------------------------
async def verify_csrf(request: Request) -> None:
    """
    Add to POST routes:  dependencies=[Depends(verify_csrf)]
    Accepts the token from the X-CSRF-Token header (JavaScript fetch) or the
    hidden `csrf_token` form field (normal HTML forms and file uploads).
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return

    token = request.headers.get(CSRF_HEADER)
    if not token:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()   # Starlette caches this; the endpoint can still read it
            value = form.get(CSRF_FORM_FIELD)
            token = value if isinstance(value, str) else None

    if not csrf_token_is_valid(request, token):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Your session expired or the form is out of date. Please reload the page and try again.",
        )
