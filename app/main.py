"""
app/main.py
-----------
Creates the FastAPI application - the equivalent of Program.cs in ASP.NET Core:
middleware pipeline, error handling, static files, startup tasks and routes.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import routes as routes_package
from app.config import STATIC_DIR, settings
from app.db import init_db
from app.deps import (
    CHANGE_PASSWORD_PATH,
    DbSession,
    OptionalUser,
    RedirectException,
    home_url_for,
)
from app.seed import print_admin_otp, seed_database
from app.templating import render

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(name)s: %(message)s")
logger = logging.getLogger("paramount")

# Largest request body we accept: max upload + 1 MB for the other form fields.
MAX_BODY_BYTES = settings.max_upload_bytes + 1024 * 1024

# Content Security Policy: only our own scripts/styles/images may load.
# No inline <script>, no inline style="" attributes, no CDNs.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
        "frame-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    ]
)

ERROR_TITLES = {
    400: "Something's not right",
    401: "Please log in",
    403: "Access denied",
    404: "Page not found",
    405: "Not allowed",
    413: "File too large",
    429: "Too many requests",
    500: "Something went wrong",
}


# ===========================================================================
# Startup / shutdown  (like IHostedService start-up work)
# ===========================================================================
@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()                          # create missing tables
    summary = seed_database()          # settings, admin, default subjects
    if summary["admin_otp"]:
        print_admin_otp(summary["admin_otp"], "Admin account created")
    if settings.secret_key.startswith("dev-only"):
        logger.warning("Using the development SECRET_KEY - set SECRET_KEY before going live.")
    yield                              # the app runs here
    # (nothing to clean up on shutdown)


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    # Swagger UI only in development (handy for exploring JSON endpoints).
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)


# ===========================================================================
# Middleware. Starlette runs the LAST added middleware FIRST, so the order is:
#   SessionMiddleware -> GZip -> security_headers -> your route
# ===========================================================================
@app.middleware("http")
async def security_headers(request: Request, call_next) -> Response:
    # Reject oversized uploads before reading them into memory.
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
        return PlainTextResponse("The file is too large (maximum 10 MB).", status_code=413)

    response = await call_next(request)
    h = response.headers
    h.setdefault("X-Content-Type-Options", "nosniff")
    h.setdefault("Referrer-Policy", "same-origin")
    h.setdefault("X-Frame-Options", "SAMEORIGIN")
    h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")

    if h.get("content-type", "").startswith("text/html"):
        h.setdefault("Content-Security-Policy", CSP)
        # Pages contain personal data: never stored by shared proxies.
        h.setdefault("Cache-Control", "private, no-cache")
    elif request.url.path.startswith("/static/") and "v" in request.query_params:
        # Versioned static files (?v=...) never change, so cache them for a year.
        h.setdefault("Cache-Control", "public, max-age=31536000, immutable")

    if settings.is_production:
        h.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    session_cookie="tpa_session",
    max_age=settings.session_max_age,
    same_site="lax",                     # cookie not sent on cross-site POSTs
    https_only=settings.is_production,   # Secure flag in production
)


# ===========================================================================
# Error handling  (like UseExceptionHandler / UseStatusCodePages)
# ===========================================================================
def _wants_json(request: Request) -> bool:
    """True for JavaScript fetch calls (marks autosave etc.)."""
    accept = request.headers.get("accept", "")
    content_type = request.headers.get("content-type", "")
    return "application/json" in accept or content_type.startswith("application/json")


def _error_response(
    request: Request, status_code: int, message: str, headers: dict | None = None
) -> Response:
    if _wants_json(request):
        return JSONResponse({"error": message}, status_code=status_code, headers=headers)
    try:
        return render(
            request,
            "error.html",
            {
                "status_code": status_code,
                "title": ERROR_TITLES.get(status_code, "Error"),
                "message": message,
                "layout": "auth",
            },
            status_code=status_code,
            headers=headers,
        )
    except Exception:   # e.g. error.html missing - never fail while showing an error
        logger.exception("Could not render the error page")
        return PlainTextResponse(f"{status_code} - {message}", status_code=status_code, headers=headers)


@app.exception_handler(RedirectException)
async def handle_redirect(request: Request, exc: RedirectException) -> Response:
    if _wants_json(request):
        return JSONResponse({"error": "login_required", "redirect": exc.url}, status_code=401)
    return RedirectResponse(exc.url, status_code=303)


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code == 404:
        message = "The page you're looking for doesn't exist or has moved."
    elif exc.status_code == 405:
        message = "That action isn't allowed here."
    else:
        message = str(exc.detail)
    return _error_response(request, exc.status_code, message, getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def handle_validation_error(request: Request, exc: RequestValidationError) -> Response:
    if _wants_json(request):
        return JSONResponse({"error": "Invalid data", "details": exc.errors()}, status_code=422)
    return _error_response(request, 400, "Some of the information sent was missing or invalid.")


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception) -> Response:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _error_response(request, 500, "Something went wrong on our side. Please try again.")


# ===========================================================================
# Static files and app-level endpoints
# ===========================================================================
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _static_file(name: str, media_type: str, headers: dict | None = None) -> FileResponse:
    path = STATIC_DIR / name
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type=media_type, headers=headers)


@app.get("/", include_in_schema=False)
def index(user: OptionalUser) -> RedirectResponse:
    """Send everyone to the right starting page."""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.must_change_password:
        return RedirectResponse(CHANGE_PASSWORD_PATH, status_code=303)
    return RedirectResponse(home_url_for(user), status_code=303)


@app.get("/healthz", include_in_schema=False)
def healthz(db: DbSession) -> dict:
    """Uptime check: also confirms the database answers."""
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


# The service worker must be served from the site root to control every page.
@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return _static_file(
        "sw.js",
        "text/javascript",
        {"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return _static_file("manifest.webmanifest", "application/manifest+json")


@app.get("/offline.html", include_in_schema=False)
def offline_page() -> FileResponse:
    return _static_file("offline.html", "text/html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return _static_file("icons/favicon.ico", "image/x-icon")


@app.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nDisallow: /\n")   # private app: don't index


# ===========================================================================
# Routes: every module in app/routes/ that defines `router` is included
# automatically (like controller discovery in ASP.NET).
# ===========================================================================
def include_all_routers(application: FastAPI) -> None:
    modules = sorted(pkgutil.iter_modules(routes_package.__path__), key=lambda m: m.name)
    for module_info in modules:
        module = importlib.import_module(f"{routes_package.__name__}.{module_info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            application.include_router(router)
            logger.info("Routes loaded from app/routes/%s.py", module_info.name)


include_all_routers(app)
