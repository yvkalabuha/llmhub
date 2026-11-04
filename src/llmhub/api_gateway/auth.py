"""Client keys (sha256 only), principals, admin key, async FastAPI dependencies."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import signal
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Request

from llmhub.api_gateway.errors import ApiError, ErrorCode
from llmhub.clock import Clock
from llmhub.configuration.api_models import Policy
from llmhub.configuration.loader import ConfigError, load_config
from llmhub.configuration.models import Config
from llmhub.persistence.db import tx
from llmhub.task_queue.models import Priority

log = logging.getLogger("llmhub.api.auth")

if TYPE_CHECKING:
    from llmhub.api_gateway.app import Runtime


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> str:
    return "lh_" + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Principal:
    client_id: str
    allowed_models: frozenset[str]
    max_priority: Priority
    default_priority: Priority
    default_policy: Policy
    cloud_allowed: bool

    def may_use(self, alias: str) -> bool:
        return "*" in self.allowed_models or alias in self.allowed_models


class ClientRegistry:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, clock: Clock) -> None:
        self.conn = conn
        self.cfg = cfg
        self.clock = clock
        self._by_hash: dict[str, Principal] = {}

    def sync(self) -> None:
        """Config -> `clients` table (upsert, absent clients disabled) and the key index."""
        now = self.clock.now()
        index: dict[str, Principal] = {}
        with tx(self.conn):
            for name, c in self.cfg.clients.items():
                self.conn.execute(
                    "INSERT INTO clients(id, name, key_hash, default_policy, default_priority,"
                    " max_priority, allowed_models_json, data_class, created_at, disabled_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,NULL)"
                    " ON CONFLICT(id) DO UPDATE SET key_hash=excluded.key_hash,"
                    " default_policy=excluded.default_policy,"
                    " default_priority=excluded.default_priority,"
                    " max_priority=excluded.max_priority,"
                    " allowed_models_json=excluded.allowed_models_json,"
                    " data_class=excluded.data_class, disabled_at=NULL",
                    (name, name, c.key_sha256, c.default_policy, c.default_priority,
                     c.max_priority, json.dumps(sorted(c.allowed_models)),
                     "cloud_allowed" if c.cloud_allowed else "private", now),
                )  # fmt: skip
                index[c.key_sha256] = Principal(
                    client_id=name,
                    allowed_models=frozenset(c.allowed_models),
                    max_priority=c.max_priority,
                    default_priority=c.default_priority,
                    default_policy=c.default_policy,
                    cloud_allowed=c.cloud_allowed,
                )
            names = list(self.cfg.clients)
            marks = ",".join("?" for _ in names)
            self.conn.execute(
                f"UPDATE clients SET disabled_at=? WHERE disabled_at IS NULL AND id NOT IN ({marks})",
                (now, *names),
            )
        self._by_hash = index

    def reload(self, path: Path) -> int:
        """Re-read the config file, validate it, replace the client set. A broken file keeps
        the old set (ConfigError propagates, nothing changes)."""
        new = load_config(Path(path))
        self.cfg.clients = new.clients
        self.sync()
        return len(new.clients)

    def by_key(self, key: str) -> Principal | None:
        digest = hash_key(key)
        found: Principal | None = None
        for h, p in self._by_hash.items():  # constant-time compare over all entries
            if hmac.compare_digest(h, digest):
                found = p
        return found

    def is_client_key(self, key: str) -> bool:
        return self.by_key(key) is not None


def install_sighup(loop: asyncio.AbstractEventLoop, registry: ClientRegistry, path: Path) -> None:
    """SIGHUP re-reads the `clients` section of the config file (errors keep the old set)."""

    def handler() -> None:
        try:
            n = registry.reload(path)
            log.info("clients reloaded: %d", n)
        except (ConfigError, OSError) as exc:
            log.error("clients reload rejected: %s", type(exc).__name__)

    loop.add_signal_handler(signal.SIGHUP, handler)


def bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def runtime(request: Request) -> Runtime:
    rt: Runtime = request.app.state.rt
    return rt


async def require_client(request: Request) -> Principal:
    key = bearer(request)
    if key is None:
        raise ApiError(ErrorCode.UNAUTHORIZED, "missing API key")
    principal = runtime(request).registry.by_key(key)
    if principal is None:
        raise ApiError(ErrorCode.UNAUTHORIZED, "invalid API key")
    return principal


async def require_admin(request: Request) -> None:
    rt = runtime(request)
    admin = rt.cfg.api.admin_key_sha256
    if admin is None:
        raise ApiError(ErrorCode.FORBIDDEN, "admin API disabled", reason="admin_disabled")
    key = bearer(request)
    if key is None:
        raise ApiError(ErrorCode.UNAUTHORIZED, "missing admin key")
    if hmac.compare_digest(hash_key(key), admin):
        return
    if rt.registry.is_client_key(key):
        raise ApiError(ErrorCode.FORBIDDEN, "client key cannot use the admin API")
    raise ApiError(ErrorCode.UNAUTHORIZED, "invalid admin key")


__all__ = [
    "ClientRegistry", "ConfigError", "Principal", "generate_key", "hash_key", "install_sighup",
    "require_admin", "require_client",
]  # fmt: skip
