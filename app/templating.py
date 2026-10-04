"""
app/templating.py
-----------------
Shared Jinja2 setup used by every route - roughly Razor's _ViewImports.cshtml
plus a base view model.

    from app.templating import render, flash
    return render(request, "student/home.html", {"notices": notices})

render() always adds: app_name, current_user, csrf_token, toast messages
("flashes"), the layout to use and the navigation menu for the user's role.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    STANDARDS,
    STATIC_DIR,
    STREAMS,
    TEMPLATES_DIR,
    current_academic_year,
    settings,
    standard_label,
    to_ist,
)
from app.security import get_csrf_token

# Autoescape is ON (Starlette's default): {{ value }} is HTML-encoded,
# exactly like @value in Razor. Never mark user input as |safe.
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ===========================================================================
# Formatting helpers (registered as Jinja filters below)
# ===========================================================================
def format_inr(paise: int | None) -> str:
    """125050 -> '₹1,250.50'; 1234567800 -> '₹1,23,45,678' (Indian grouping)."""
    if paise is None:
        return "-"
    sign = "-" if paise < 0 else ""
    rupees, rest = divmod(abs(int(paise)), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:          # lakhs/crores: groups of two after the first three
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}₹{digits}" + (f".{rest:02d}" if rest else "")


def format_ist_datetime(value: datetime | None, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    """Stored UTC timestamp -> '04 Oct 2026, 03:45 PM' in Indian time."""
    local = to_ist(value)
    return local.strftime(fmt) if local else "-"


def format_date(value: date | None, fmt: str = "%d %b %Y") -> str:
    """date -> '04 Oct 2026'."""
    return value.strftime(fmt) if value else "-"


def format_number(value: float | None, decimals: int = 1) -> str:
    """42.0 -> '42', 42.5 -> '42.5', None -> '-'."""
    if value is None:
        return "-"
    text = f"{value:.{decimals}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_filesize(size: int | None) -> str:
    """1536 -> '1.5 KB', 2_400_000 -> '2.3 MB'."""
    if not size:
        return "0 KB"
    if size < 1024 * 1024:
        return f"{max(size / 1024, 0.1):.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


_ONES = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
         "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _words_below_1000(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100] + " hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def format_inr_words(paise: int | None) -> str:
    """125050 -> 'Rupees one thousand two hundred fifty and fifty paise only' (Indian system)."""
    if not paise:
        return "Rupees zero only"
    rupees, rest = divmod(int(paise), 100)
    parts = []
    for size, name in ((10_000_000, "crore"), (100_000, "lakh"), (1000, "thousand")):
        if rupees >= size:
            parts.append(_words_below_1000(rupees // size) + " " + name)
            rupees %= size
    if rupees:
        parts.append(_words_below_1000(rupees))
    text = "Rupees " + (" ".join(parts) or "zero")
    if rest:
        text += f" and {_words_below_1000(rest)} paise"
    return text + " only"


# ===========================================================================
# Static file URLs with cache-busting:  /static/app.css?v=1759561234
# The version is the file's last-modified time, so browsers (and the service
# worker) fetch a fresh copy whenever the file changes.
# ===========================================================================
_static_versions: dict[str, str] = {}


def static_url(path: str) -> str:
    if settings.is_production and path in _static_versions:
        version = _static_versions[path]
    else:
        try:
            version = str(int((STATIC_DIR / path).stat().st_mtime))
        except OSError:
            version = "0"
        _static_versions[path] = version
    return f"/static/{path}?v={version}"


# ===========================================================================
# Navigation per role: (label, url, icon name). Icons live in macros.html.
# ===========================================================================
NAV: dict[str, list[tuple[str, str, str]]] = {
    "admin": [
        ("Dashboard", "/admin", "home"),
        ("Students", "/staff/students", "users"),
        ("Teachers", "/admin/teachers", "user"),
        ("Subjects", "/admin/subjects", "layers"),
        ("Course notes", "/staff/notes", "book"),
        ("Exams & marks", "/staff/exams", "clipboard"),
        ("Fees", "/admin/fees", "wallet"),
        ("Notices", "/staff/notices", "megaphone"),
        ("Promote", "/admin/promote", "arrow-up"),
        ("Audit log", "/admin/audit", "shield"),
        ("Settings", "/admin/settings", "settings"),
    ],
    "teacher": [
        ("Home", "/staff", "home"),
        ("Students", "/staff/students", "users"),
        ("Course notes", "/staff/notes", "book"),
        ("Exams & marks", "/staff/exams", "clipboard"),
        ("Notices", "/staff/notices", "megaphone"),
    ],
    "student": [   # the bottom tab bar
        ("Home", "/student", "home"),
        ("Courses", "/student/courses", "book"),
        ("Results", "/student/results", "chart"),
        ("Ranks", "/student/ranks", "trophy"),
        ("Fees", "/student/fees", "wallet"),
    ],
}
HOME_URLS = {"/admin", "/staff", "/student"}   # matched exactly, not as a prefix


def _nav_for(user: Any, path: str) -> list[dict[str, Any]]:
    if user is None or user.must_change_password:
        return []
    items = []
    for label, url, icon in NAV.get(user.role, []):
        active = path == url if url in HOME_URLS else (path == url or path.startswith(url + "/"))
        items.append({"label": label, "url": url, "icon": icon, "active": active})
    return items


def _default_layout(user: Any) -> str:
    """'auth' = centred card, 'staff' = sidebar, 'student' = bottom tab bar."""
    if user is None or user.must_change_password:
        return "auth"
    return "student" if user.role == "student" else "staff"


# ===========================================================================
# Toast messages ("flash" messages survive one redirect via the session)
# ===========================================================================
FLASH_KEY = "_flashes"


def flash(request: Request, text: str, kind: str = "success") -> None:
    """Queue a toast for the next page. kind: 'success', 'error' or 'info'."""
    items = list(request.session.get(FLASH_KEY, []))
    items.append({"kind": kind, "text": text})
    request.session[FLASH_KEY] = items[-5:]      # keep at most 5


def _pop_flashes(request: Request) -> list[dict[str, str]]:
    return request.session.pop(FLASH_KEY, [])


def redirect(request: Request, url: str, message: str | None = None, kind: str = "success") -> RedirectResponse:
    """Post/Redirect/Get: optional toast, then a 303 redirect (refresh never re-posts)."""
    if message:
        flash(request, message, kind)
    return RedirectResponse(url, status_code=303)


# ===========================================================================
# render() - use this instead of templates.TemplateResponse directly
# ===========================================================================
def render(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
):
    # request.state.user is set by deps.get_optional_user when a route used it.
    user = getattr(request.state, "user", None)
    data: dict[str, Any] = {
        "app_name": settings.app_name,
        "current_user": user,
        "csrf_token": get_csrf_token(request),
        "flashes": _pop_flashes(request),
        "layout": _default_layout(user),
        "nav_items": _nav_for(user, request.url.path),
        "academic_year": current_academic_year(),
    }
    if context:
        data.update(context)      # page values win (e.g. layout="auth")
    return templates.TemplateResponse(
        request, template_name, data, status_code=status_code, headers=headers
    )


# Make helpers available inside every template.
templates.env.globals.update(
    static_url=static_url,
    standard_label=standard_label,
    STANDARDS=STANDARDS,
    STREAMS=STREAMS,
)
templates.env.filters.update(
    inr=format_inr,
    ist=format_ist_datetime,
    nice_date=format_date,
    num=format_number,
    filesize=format_filesize,
    inr_words=format_inr_words,
)
