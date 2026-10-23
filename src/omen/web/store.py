"""Small key-value store for the website: Upstash Redis over its REST API (free tier, via the Vercel Marketplace), or
an in-process fallback so the site still works (without the Pi feed or shared history) before storage is added.

Upstash env names: ``KV_REST_API_URL``/``KV_REST_API_TOKEN`` (Vercel Marketplace) or
``UPSTASH_REDIS_REST_URL``/``UPSTASH_REDIS_REST_TOKEN``.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx


class StoreError(RuntimeError):
    pass


class MemoryStore:
    kind = "memory"

    def __init__(self) -> None:
        self.data: dict[str, tuple[Any, float | None]] = {}
        self.lock = threading.Lock()

    def _get(self, key: str) -> Any:
        item = self.data.get(key)
        if item is None:
            return None
        value, exp = item
        if exp is not None and exp < time.time():
            self.data.pop(key, None)
            return None
        return value

    def get(self, key: str) -> str | None:
        with self.lock:
            return self._get(key)

    def mget(self, keys: list[str]) -> list[str | None]:
        with self.lock:
            return [self._get(k) for k in keys]

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        with self.lock:
            if nx and self._get(key) is not None:
                return False
            self.data[key] = (value, time.time() + ex if ex else None)
            return True

    def delete(self, key: str) -> None:
        with self.lock:
            self.data.pop(key, None)

    def incr(self, key: str, by: float = 1, ex: int | None = None) -> float:
        with self.lock:
            current = float(self._get(key) or 0) + by
            exp = self.data.get(key, (None, None))[1] or (time.time() + ex if ex else None)
            self.data[key] = (str(current), exp)
            return current

    def lpush_trim(self, key: str, value: str, maxlen: int) -> None:
        with self.lock:
            lst = list(self._get(key) or [])
            lst.insert(0, value)
            self.data[key] = (lst[:maxlen], None)

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        with self.lock:
            lst = list(self._get(key) or [])
            return lst[start : (None if stop == -1 else stop + 1)]


class UpstashStore:
    kind = "upstash"

    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self.client = httpx.Client(timeout=10, transport=transport, headers={"Authorization": f"Bearer {token}"})

    def _cmd(self, *args: Any) -> Any:
        try:
            resp = self.client.post(self.url, json=[str(a) for a in args])
        except httpx.HTTPError as exc:
            raise StoreError(f"store unreachable: {exc}") from exc
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or "error" in data:
            raise StoreError(f"store error: {data.get('error', resp.status_code)}")
        return data.get("result")

    def _pipeline(self, commands: list[list[Any]]) -> list[Any]:
        try:
            resp = self.client.post(f"{self.url}/pipeline", json=[[str(a) for a in c] for c in commands])
        except httpx.HTTPError as exc:
            raise StoreError(f"store unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise StoreError(f"store error: HTTP {resp.status_code}")
        out = []
        for item in resp.json():
            if "error" in item:
                raise StoreError(f"store error: {item['error']}")
            out.append(item.get("result"))
        return out

    def get(self, key: str) -> str | None:
        return self._cmd("GET", key)

    def mget(self, keys: list[str]) -> list[str | None]:
        return list(self._cmd("MGET", *keys) or [None] * len(keys))

    def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        args: list[Any] = ["SET", key, value]
        if ex:
            args += ["EX", ex]
        if nx:
            args.append("NX")
        return self._cmd(*args) == "OK"

    def delete(self, key: str) -> None:
        self._cmd("DEL", key)

    def incr(self, key: str, by: float = 1, ex: int | None = None) -> float:
        cmds: list[list[Any]] = [["INCRBYFLOAT", key, by]]
        if ex:
            cmds.append(["EXPIRE", key, ex, "NX"])
        return float(self._pipeline(cmds)[0])

    def lpush_trim(self, key: str, value: str, maxlen: int) -> None:
        self._pipeline([["LPUSH", key, value], ["LTRIM", key, 0, maxlen - 1]])

    def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return list(self._cmd("LRANGE", key, start, stop) or [])


_STORE: MemoryStore | UpstashStore | None = None


def get_store(env: dict[str, str] | None = None) -> MemoryStore | UpstashStore:
    global _STORE
    if _STORE is None:
        e = os.environ if env is None else env
        url = e.get("KV_REST_API_URL") or e.get("UPSTASH_REDIS_REST_URL")
        token = e.get("KV_REST_API_TOKEN") or e.get("UPSTASH_REDIS_REST_TOKEN")
        _STORE = UpstashStore(url, token) if url and token else MemoryStore()
    return _STORE


def reset_store(store: MemoryStore | UpstashStore | None = None) -> None:
    global _STORE
    _STORE = store
