"""Run the website on your own machine: the API plus the built frontend (web/dist) from one process.

``omen web`` is for previewing and development; production runs on Vercel (api/index.py + vercel.json).
"""

from __future__ import annotations

import json
import mimetypes
import os
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def site_headers(config: Path = ROOT / "vercel.json") -> list[tuple[bytes, bytes]]:
    """The security headers Vercel adds to every page (vercel.json), so local previews behave the same (CSP etc.)."""
    try:
        rules = json.loads(config.read_text()).get("headers", [])
    except (OSError, ValueError):
        return []
    for rule in rules:
        if rule.get("source") == "/(.*)":
            return [(h["key"].lower().encode(), h["value"].encode()) for h in rule.get("headers", [])]
    return []


def with_static(api_app: Callable, dist: Path) -> Callable:
    """Serve /api/* from the API app and everything else from the built single-page app."""
    dist = dist.resolve()
    extra = site_headers()

    async def app(scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or scope.get("path", "/").startswith("/api"):
            await api_app(scope, receive, send)
            return
        rel = scope.get("path", "/").lstrip("/") or "index.html"
        file = (dist / rel).resolve()
        if dist not in file.parents or not file.is_file():
            file = dist / "index.html"  # client-side routes and unknown paths fall back to the app shell
        if not file.is_file():
            body = b"web/dist is missing: run `npm --prefix web ci && npm --prefix web run build` first"
            await send({"type": "http.response.start", "status": 503, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": body})
            return
        ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        cache = b"public, max-age=31536000, immutable" if "/assets/" in f"/{rel}" else b"no-cache"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", ctype.encode()), (b"cache-control", cache), *extra],
            }
        )
        await send({"type": "http.response.body", "body": file.read_bytes()})

    return app


def serve(host: str, port: int, dist: Path | None = None) -> None:
    try:
        import uvicorn
    except ImportError as exc:  # dev-only dependency
        raise SystemExit("`omen web` needs uvicorn: `uv sync` (dev group) or `pip install uvicorn`") from exc
    if host in {"127.0.0.1", "localhost", "::1"}:
        os.environ.setdefault("OMEN_WEB_INSECURE", "1")  # plain http on localhost: cookies can't be Secure
    from omen.web.app import app

    uvicorn.run(with_static(app, dist or ROOT / "web" / "dist"), host=host, port=port, log_level="info")
