"""
app/routes/auth.py
------------------
Login, logout and change password (the same pages for every role).

    GET  /login             login form
    POST /login             check ID + password, start session
    POST /logout            end session
    GET  /change-password   form (forced after a one-time password)
    POST /change-password   save the new password
"""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.config import utcnow
from app.deps import (
    CHANGE_PASSWORD_PATH,
    CurrentUser,
    DbSession,
    OptionalUser,
    home_url_for,
    verify_csrf,
)
from app.models import AuditLog, User
from app.security import (
    burn_password_check,
    hash_password,
    login_limiter,
    login_session,
    logout_session,
    needs_rehash,
    normalise_login_id,
    safe_next_url,
    validate_new_password,
    verify_password,
)
from app.templating import flash, render

router = APIRouter(tags=["auth"])
logger = logging.getLogger("paramount.auth")

# One message for "unknown ID" and "wrong password", so nobody can find out
# which login IDs exist.
GENERIC_LOGIN_ERROR = "Incorrect login ID or password."


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
def _login_page(
    request: Request,
    *,
    login_id: str = "",
    next_url: str = "",
    error: str | None = None,
    info: str | None = None,
    status_code: int = 200,
):
    return render(
        request,
        "login.html",
        {
            "login_id": login_id,
            "next_url": safe_next_url(next_url, ""),
            "error": error,
            "info": info,
            "layout": "auth",
        },
        status_code=status_code,
    )


@router.get("/login")
def login_page(
    request: Request,
    user: OptionalUser,
    next_url: Annotated[str | None, Query(alias="next")] = None,
    logged_out: int = 0,
):
    # Already logged in? Go straight to the right page.
    if user is not None:
        target = CHANGE_PASSWORD_PATH if user.must_change_password else home_url_for(user)
        return RedirectResponse(target, status_code=303)
    info = "You have been logged out." if logged_out else None
    return _login_page(request, next_url=next_url or "", info=info)


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login_submit(
    request: Request,
    db: DbSession,
    login_id: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next_url: Annotated[str, Form(alias="next")] = "",
):
    login_id = normalise_login_id(login_id)

    def fail(message: str, status_code: int = 400):
        return _login_page(
            request, login_id=login_id, next_url=next_url, error=message, status_code=status_code
        )

    if not login_id or not password:
        return fail("Please enter your login ID and password.")

    # 1. Rate limit: max 5 failures per ID per minute.
    wait = login_limiter.seconds_blocked(login_id)
    if wait:
        return fail(f"Too many failed attempts. Please wait {wait} seconds and try again.", 429)

    # 2. Look up the user and check the password.
    user = db.scalar(select(User).where(User.login_id == login_id))
    if user is None:
        burn_password_check(password)          # same delay as a real check
        login_limiter.record_failure(login_id)
        return fail(GENERIC_LOGIN_ERROR)

    if not verify_password(password, user.password_hash):
        login_limiter.record_failure(login_id)
        logger.info("Failed login for %s", login_id)
        return fail(GENERIC_LOGIN_ERROR)

    # Only reveal "inactive" once the password was correct.
    if not user.is_active:
        return fail("This account is inactive. Please contact the academy office.", 403)

    # 3. Success.
    login_limiter.reset(login_id)
    if needs_rehash(user.password_hash):       # upgrade old hashes silently
        user.password_hash = hash_password(password)
    user.last_login_at = utcnow()
    db.commit()

    login_session(request, user)
    if user.must_change_password:
        return RedirectResponse(CHANGE_PASSWORD_PATH, status_code=303)
    return RedirectResponse(safe_next_url(next_url, home_url_for(user)), status_code=303)


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------
@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    logout_session(request)
    response = RedirectResponse("/login?logged_out=1", status_code=303)
    # Asks supporting browsers to drop cached copies of pages. The service
    # worker (Phase 7) also clears its own saved pages on logout.
    response.headers["Clear-Site-Data"] = '"cache"'
    return response


# ---------------------------------------------------------------------------
# Change password
# ---------------------------------------------------------------------------
@router.get("/change-password")
def change_password_page(request: Request, user: CurrentUser):
    return render(
        request,
        "change_password.html",
        {"forced": user.must_change_password, "errors": []},
    )


@router.post("/change-password", dependencies=[Depends(verify_csrf)])
def change_password_submit(
    request: Request,
    db: DbSession,
    user: CurrentUser,
    current_password: Annotated[str, Form()] = "",
    new_password: Annotated[str, Form()] = "",
    confirm_password: Annotated[str, Form()] = "",
):
    forced = user.must_change_password
    errors: list[str] = []

    # Wrong "current password" guesses count towards the same rate limit.
    wait = login_limiter.seconds_blocked(user.login_id)
    if wait:
        errors.append(f"Too many failed attempts. Please wait {wait} seconds and try again.")
    elif not verify_password(current_password, user.password_hash):
        login_limiter.record_failure(user.login_id)
        errors.append(
            "The one-time password is incorrect." if forced else "Your current password is incorrect."
        )

    if new_password != confirm_password:
        errors.append("The new passwords do not match.")
    errors.extend(validate_new_password(new_password, user.login_id))
    if new_password and new_password == current_password:
        errors.append("Choose a password different from the current one.")

    if errors:
        return render(
            request,
            "change_password.html",
            {"forced": forced, "errors": errors},
            status_code=400,
        )

    # Save: new hash, clear the "must change" flag, log out other devices.
    login_limiter.reset(user.login_id)
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    user.password_changed_at = utcnow()
    user.session_version += 1
    db.add(
        AuditLog(
            user_id=user.id,
            actor_login_id=user.login_id,
            action="password.change",
            entity="user",
            entity_id=user.login_id,
            details=json.dumps({"after_one_time_password": forced}),
        )
    )
    db.commit()

    login_session(request, user)       # keep THIS device logged in (new session_version)
    flash(request, "Password updated successfully.")
    return RedirectResponse(home_url_for(user), status_code=303)
