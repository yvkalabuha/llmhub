"""Operator CLI over the admin HTTP API (T-371 B): `python -m llmhub <command> ...`.

Only an HTTP client (httpx) plus a few local file operations; it never imports the controller.
The admin key comes from a file (`--admin-key-file`, default `/etc/llmhub/admin.key`), or from
`/dev/stdin` in a container: `docker exec -i llmhub-api python -m llmhub status
--admin-key-file /dev/stdin < /etc/llmhub/admin.key`. The key is never put in argv, env, logs
or output.

Exit codes: 0 ok, 1 the API refused / is unreachable, 2 arguments or config.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from llmhub.configuration.loader import ConfigError, load_config
from llmhub.configuration.models import Config

DEFAULT_CONFIG = "/etc/llmhub/conf/config.yaml"
DEFAULT_ADMIN_KEY = "/etc/llmhub/admin.key"
STDIN = "/dev/stdin"
KILL_FILE = "KILL"  # same name as llmhub.killswitch.KILL_FILE (no controller import here)
_KEY_MAX = 4096


class AdminCliError(Exception):
    """A refused/failed call; the message names the HTTP status and error code only."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def read_key_file(path: Path) -> str:
    """First line of the admin key file. `/dev/stdin` is read from stdin; any other path must
    be a regular file (no symlink) without group/other access (mode & 0o077 == 0)."""
    if str(path) == STDIN:
        line = sys.stdin.readline(_KEY_MAX).strip()
        if not line:
            raise AdminCliError("admin key: stdin is empty", exit_code=2)
        return line
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise AdminCliError(f"admin key {path}: cannot open ({type(exc).__name__})",
                            exit_code=2) from None  # fmt: skip
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AdminCliError(f"admin key {path}: must be a regular file", exit_code=2)
        if st.st_mode & 0o077:
            raise AdminCliError(f"admin key {path}: mode must be 0600", exit_code=2)
        data = os.read(fd, _KEY_MAX)
    finally:
        os.close(fd)
    line = data.split(b"\n", 1)[0].strip().decode("utf-8", "replace")
    if not line:
        raise AdminCliError(f"admin key {path}: empty", exit_code=2)
    return line


def base_url(cfg: Config, override: str | None) -> str:
    if override:
        return override.rstrip("/")
    host = cfg.api.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{cfg.api.port}"


def call(
    url: str,
    key: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: float = 70.0,
) -> dict[str, Any]:
    """One admin request; non-2xx -> AdminCliError("HTTP <status> <code>[ (<reason>)]")."""
    try:
        resp = httpx.request(
            method, url + path, json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {key}"},
        )  # fmt: skip
    except httpx.HTTPError as exc:
        raise AdminCliError(f"{method} {path}: {type(exc).__name__}") from None
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code >= 300:
        err = data.get("error") if isinstance(data, dict) else None
        code = err.get("code") if isinstance(err, dict) else None
        reason = err.get("reason") if isinstance(err, dict) else None
        msg = f"HTTP {resp.status_code} {code or ''}".rstrip()
        if reason:
            msg += f" ({reason})"
        raise AdminCliError(msg)
    if isinstance(data, dict):
        return data
    return {"text": resp.text}


def write_kill_file(data_dir: Path) -> Path:
    """`kill --file`: the KILL file works without the API (controller and reaper honour it
    within 60 s); `resume` refuses while it exists."""
    path = Path(data_dir) / KILL_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o640)
    os.close(fd)
    return path


def _print(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False))


# ------------------------------------------------------------------ admin API commands
_API: dict[str, tuple[str, str]] = {
    "status": ("GET", "/admin/status"),
    "budget": ("GET", "/admin/budget"),
    "client list": ("GET", "/admin/clients"),
    "reload-clients": ("POST", "/admin/reload_clients"),
    "hold": ("POST", "/admin/hold"),
    "release": ("POST", "/admin/release"),
    "kill": ("POST", "/admin/kill"),
    "resume": ("POST", "/admin/resume"),
    "clear-weights-hold": ("POST", "/admin/clear_weights_hold"),
}


def _target(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "pool", None):
        return {"pool": args.pool}
    if getattr(args, "model", None):
        return {"model": args.model}
    raise AdminCliError("--pool or --model is required", exit_code=2)


def _body(name: str, args: argparse.Namespace) -> dict[str, Any] | None:
    if name == "hold":
        return {**_target(args), "ttl_s": args.ttl}
    if name in ("release", "clear-weights-hold"):
        return _target(args)
    if name == "kill":
        return {"reason": args.reason} if args.reason else {}
    if name == "resume":
        return {"provider": args.provider} if args.provider else {}
    if name == "reload-clients":
        return {}
    return None


def _load(args: argparse.Namespace) -> Config:
    try:
        return load_config(Path(args.config))
    except ConfigError as exc:
        raise AdminCliError(f"config error: {exc}", exit_code=2) from None


def _api_command(name: str, args: argparse.Namespace) -> int:
    if name == "kill" and args.file:
        path = write_kill_file(_load(args).data_dir)
        _print({"kill_file": str(path)})
        return 0
    url = args.url.rstrip("/") if args.url else base_url(_load(args), None)
    key = read_key_file(Path(args.admin_key_file))
    method, path = _API[name]
    _print(call(url, key, method, path, _body(name, args), timeout=args.timeout))
    return 0


# ------------------------------------------------------------------ local commands
def _config_check(args: argparse.Namespace) -> int:
    """Config + secrets (`NAME` or `NAME_FILE`) + `mock_only` + adapters; prints `ok`."""
    from llmhub.configuration.loader import (
        INSTANCE_SECRET_ENV,
        WEIGHTS_SECRET_ENV,
        load_instance_secret,
        load_label_secret,
        load_weights_secret,
    )

    cfg = _load(args)
    env = os.environ
    try:
        load_label_secret(env)
        has = {n: n in env or n + "_FILE" in env for n in (INSTANCE_SECRET_ENV, WEIGHTS_SECRET_ENV)}
        instance = load_instance_secret(env) if has[INSTANCE_SECRET_ENV] else None
        weights = load_weights_secret(env) if has[WEIGHTS_SECRET_ENV] else None
        if instance is not None and weights is not None and instance == weights:
            raise ConfigError("LLMHUB_INSTANCE_SECRET and LLMHUB_WEIGHTS_SECRET must differ")
        uses_weights = any(p.weights is not None for p in cfg.pools.values())
        if uses_weights and (instance is None or weights is None):
            raise ConfigError("weight sets need LLMHUB_INSTANCE_SECRET and LLMHUB_WEIGHTS_SECRET")
        for name in cfg.providers.enabled:
            if name != "mock":
                raise ConfigError(f"provider {name!r} has no adapter yet (T-372)")
    except ConfigError as exc:
        raise AdminCliError(f"config error: {exc}", exit_code=2) from None
    print("ok")
    return 0


def _backup(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from llmhub.persistence.backup import backup_state_db

    os.umask(0o027)
    cfg = _load(args)
    try:
        out = backup_state_db(cfg.data_dir / "state.db", Path(args.to), args.keep,
                              datetime.now(UTC))  # fmt: skip
    except (OSError, ValueError) as exc:
        raise AdminCliError(f"backup failed: {type(exc).__name__}: {exc}") from None
    except sqlite3.Error as exc:  # locked source, copy failed integrity_check
        raise AdminCliError(f"backup failed: {type(exc).__name__}") from None
    _print({"backup": str(out)})
    return 0


def _healthcheck(args: argparse.Namespace) -> int:
    """Container healthcheck: GET url -> 0 on HTTP 200, else 1 (no key, no output on ok)."""
    try:
        r = httpx.get(args.url, timeout=args.timeout)
    except httpx.HTTPError as exc:
        print(f"unhealthy: {type(exc).__name__}", file=sys.stderr)
        return 1
    if r.status_code != 200:
        print(f"unhealthy: HTTP {r.status_code}", file=sys.stderr)
        return 1
    return 0


# ------------------------------------------------------------------ parser
def add_commands(sub: Any) -> None:
    """Register the admin/local commands on the `python -m llmhub` subparsers."""

    def api(name: str, help_: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_)
        _common(p)
        return p

    api("status", "controller status (admin API)")
    api("budget", "budget and month ledger (admin API)")
    client = sub.add_parser("client", help="clients (admin API)")
    csub = client.add_subparsers(dest="client_cmd", required=True)
    _common(csub.add_parser("list", help="configured clients"))
    api("reload-clients", "re-read the clients section of the config file")
    for name, help_ in (("hold", "keep a pool warm"), ("release", "drop the admin hold"),
                        ("clear-weights-hold", "remove a weights_config hold of a pool")):
        p = api(name, help_)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--pool")
        if name == "hold":
            g.add_argument("--model")
            p.add_argument("--ttl", type=float, required=True, help="seconds")
    k = api("kill", "stop all cloud resources (blocks up to 60 s)")
    k.add_argument("--reason", default=None)
    k.add_argument("--file", action="store_true",
                   help="write <data_dir>/KILL instead of calling the API")
    r = api("resume", "leave HALTED (refused while a KILL file exists)")
    r.add_argument("--provider", default=None)
    cc = sub.add_parser("config-check", help="validate config and secrets; prints ok")
    cc.add_argument("--config", default=DEFAULT_CONFIG)
    b = sub.add_parser("backup", help="consistent copy of state.db")
    b.add_argument("--config", default=DEFAULT_CONFIG)
    b.add_argument("--to", required=True)
    b.add_argument("--keep", type=int, default=14)
    h = sub.add_parser("healthcheck", help="GET URL, exit 0 on HTTP 200")
    h.add_argument("--url", required=True)
    h.add_argument("--timeout", type=float, default=5.0)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default=DEFAULT_CONFIG)
    p.add_argument("--url", default=None, help="override http://api.host:api.port")
    p.add_argument("--admin-key-file", default=DEFAULT_ADMIN_KEY,
                   help="file with the admin key (0600) or /dev/stdin")  # fmt: skip
    p.add_argument("--timeout", type=float, default=70.0)


COMMANDS = frozenset(
    {"status", "budget", "client", "reload-clients", "hold", "release", "kill", "resume",
     "clear-weights-hold", "config-check", "backup", "healthcheck"}
)  # fmt: skip


def dispatch(args: argparse.Namespace) -> int:
    try:
        cmd = args.cmd
        if cmd == "config-check":
            return _config_check(args)
        if cmd == "backup":
            return _backup(args)
        if cmd == "healthcheck":
            return _healthcheck(args)
        name = "client list" if cmd == "client" else cmd
        return _api_command(name, args)
    except AdminCliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code


def run(argv: Sequence[str]) -> int:
    """Stand-alone entry (tests): the same parser as `python -m llmhub`."""
    ap = argparse.ArgumentParser(prog="python -m llmhub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add_commands(sub)
    try:
        args = ap.parse_args(list(argv))
    except SystemExit as exc:
        return 2 if exc.code else 0
    return dispatch(args)
