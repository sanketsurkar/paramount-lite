"""
run.py - start the web server.

    python run.py          development: http://127.0.0.1:8000 with auto-reload
    python run.py --lan    also reachable from a phone on the same Wi-Fi
                           (http://<your-laptop-IP>:8000)

On Render/Koyeb set the start command to:  python run.py
(APP_ENV=production turns off reload, and PORT is provided by the host.)
"""
import argparse
import os

import uvicorn

from app.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Run The Paramount Academy app.")
    parser.add_argument(
        "--lan", action="store_true", help="listen on all network interfaces (phone testing)"
    )
    args = parser.parse_args()

    port = int(os.environ.get("PORT", "8000"))

    if settings.is_production:
        host, reload = "0.0.0.0", False
    else:
        host = "0.0.0.0" if args.lan else "127.0.0.1"
        reload = True          # restart automatically when a .py file changes

    uvicorn.run(
        "app.main:app",        # "module:variable" - uvicorn imports app/main.py
        host=host,
        port=port,
        reload=reload,
        reload_dirs=["app"] if reload else None,
        # Free hosts sit behind a proxy that terminates HTTPS. Trust its
        # X-Forwarded-* headers so the app knows requests were really HTTPS.
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
