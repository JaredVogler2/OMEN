"""A deliberately tiny ASGI layer (no framework dependency): routing, JSON, cookies and streamed responses.

Handlers are ordinary synchronous functions run in a worker thread, because the OMEN engine does blocking HTTP;
the event loop stays free for other requests sharing a Fluid-compute instance. A handler may return a ``Stream``
whose (synchronous) iterator yields bytes; chunks are relayed to the client as they are produced.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs

log = logging.getLogger(__name__)

MAX_BODY = 1_000_000


class HTTPError(Exception):
    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    client: str

    def arg(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPError(400, "request body is not valid JSON") from exc

    @property
    def cookies(self) -> dict[str, str]:
        jar: SimpleCookie = SimpleCookie()
        try:
            jar.load(self.headers.get("cookie", ""))
        except Exception:  # malformed cookie headers are treated as no cookies
            return {}
        return {k: v.value for k, v in jar.items()}


@dataclass
class Response:
    body: bytes = b""
    status: int = 200
    content_type: str = "application/json"
    headers: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Stream:
    chunks: Iterable[bytes]
    status: int = 200
    content_type: str = "application/x-ndjson"
    headers: list[tuple[str, str]] = field(default_factory=list)


def json_response(data: Any, status: int = 200, headers: list[tuple[str, str]] | None = None) -> Response:
    return Response(
        json.dumps(data, default=str, separators=(",", ":")).encode(), status, "application/json", list(headers or [])
    )


Handler = Callable[[Request], Response | Stream]


class App:
    def __init__(self, path_param: str = "__path", security_headers: list[tuple[str, str]] | None = None):
        self.routes: dict[tuple[str, str], Handler] = {}
        self.path_param = path_param
        self.security_headers = security_headers or []

    def route(self, method: str, path: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.routes[(method.upper(), path)] = fn
            return fn

        return deco

    def resolve_path(self, scope: dict, query: dict[str, list[str]]) -> str:
        # Vercel rewrites /api/<anything> to this single function and passes the original tail as ?__path=
        tail = query.pop(self.path_param, None)
        if tail:
            return "/api/" + tail[0].strip("/")
        path = scope.get("path") or "/"
        return path.rstrip("/") or "/"

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        query = parse_qs(scope.get("query_string", b"").decode(), keep_blank_values=True)
        headers = {k.decode().lower(): v.decode(errors="replace") for k, v in scope.get("headers", [])}
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if len(body) > MAX_BODY:
                await self._send(send, json_response({"error": "request too large"}, 413))
                return
            if not msg.get("more_body"):
                break
        client = headers.get("x-forwarded-for", "").split(",")[0].strip() or (scope.get("client") or ("?",))[0]
        req = Request(scope["method"].upper(), self.resolve_path(scope, query), query, headers, body, client)
        handler = self.routes.get((req.method, req.path))
        if handler is None:
            allowed = [m for (m, p) in self.routes if p == req.path]
            status = 405 if allowed else 404
            await self._send(send, json_response({"error": "method not allowed" if allowed else "not found"}, status))
            return
        try:
            result = await asyncio.to_thread(handler, req)
        except HTTPError as exc:
            result = json_response({"error": exc.message, **exc.extra}, exc.status)
        except Exception:
            log.exception("unhandled error in %s %s", req.method, req.path)
            result = json_response({"error": "internal error"}, 500)
        if isinstance(result, Stream):
            await self._stream(send, result)
        else:
            await self._send(send, result)

    def _headers(self, content_type: str, extra: list[tuple[str, str]]) -> list[tuple[bytes, bytes]]:
        hdrs = [("content-type", content_type), ("cache-control", "no-store"), *self.security_headers, *extra]
        return [(k.encode(), v.encode()) for k, v in hdrs]

    async def _send(self, send: Callable, resp: Response) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": resp.status,
                "headers": self._headers(resp.content_type, resp.headers),
            }
        )
        await send({"type": "http.response.body", "body": resp.body})

    async def _stream(self, send: Callable, stream: Stream) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": stream.status,
                "headers": self._headers(stream.content_type, [("x-accel-buffering", "no"), *stream.headers]),
            }
        )
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def pump() -> None:
            try:
                for chunk in stream.chunks:
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except Exception:
                log.exception("stream producer failed")
                err = json.dumps({"type": "error", "message": "internal error"}).encode() + b"\n"
                loop.call_soon_threadsafe(queue.put_nowait, err)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=pump, daemon=True).start()
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def ndjson(events: Iterator[dict]) -> Iterator[bytes]:
    for ev in events:
        yield json.dumps(ev, default=str, separators=(",", ":")).encode() + b"\n"
