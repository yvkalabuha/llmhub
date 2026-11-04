"""`python -m llmhub <command>`.

- `serve --config PATH [--no-docs]`: the API process (controller + local worker + gateway, one
  loop). Exit 75 (EX_TEMPFAIL) when state.db stays locked (`StateDbBusy`), 2 on config errors.
  The reaper is a separate process: `python -m llmhub.reaper --config PATH`.
- `keys new [--name N] [--out FILE]`: a fresh client/admin key. Without `--out` the key is
  printed ONCE (stdout); with `--out` it goes to FILE (0600, must not exist) and stdout gets
  only its sha256 for `clients.<name>.key_sha256` / `api.admin_key_sha256`.
- operator commands over the admin API and local helpers (`llmhub.admin_cli`): status, budget,
  client list, reload-clients, hold, release, kill, resume, clear-weights-hold, config-check,
  backup, healthcheck; T-372 live-test helpers: ledger-exclude, provider-check,
  lifecycle-probe, scan-secrets, weights-token-check.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path

EX_TEMPFAIL = 75


def _serve(args: argparse.Namespace) -> int:
    from llmhub.api_gateway.logging import configure_logging
    from llmhub.clock import SystemClock
    from llmhub.configuration.loader import ConfigError, load_config
    from llmhub.persistence.integrity import StateDbBusy
    from llmhub.server import apply_umask, build_service, serve

    configure_logging(args.log_level)
    apply_umask()  # before state.db, bodies and backups are created
    path = Path(args.config)
    try:
        cfg = load_config(path)
        if args.no_docs:
            cfg.api.docs = False
        svc = build_service(cfg, os.environ, SystemClock(), config_path=path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except StateDbBusy as exc:
        print(f"state.db busy: {exc}; exiting {EX_TEMPFAIL} (restart retries)", file=sys.stderr)
        return EX_TEMPFAIL
    return asyncio.run(serve(svc, port=args.port))


def _write_new_key(path: Path, key: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (key + "\n").encode())
    finally:
        os.close(fd)


def _keys_new(args: argparse.Namespace) -> int:
    from llmhub.api_gateway.auth import generate_key, hash_key

    key = generate_key()
    digest = hash_key(key)
    if args.out:
        try:
            _write_new_key(Path(args.out), key)
        except FileExistsError:
            print(f"error: {args.out} exists; not overwritten", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: cannot write {args.out} ({type(exc).__name__})", file=sys.stderr)
            return 2
    else:
        print(f"key: {key}")
    print(f"key_sha256: {digest}")
    if args.name:
        print(f'# config: clients.{args.name}.key_sha256: "{digest}"')
    if not args.out:
        print("# the key is shown once; store it in the client's secret store", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from llmhub import admin_cli

    ap = argparse.ArgumentParser(prog="python -m llmhub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="run the API process")
    sp.add_argument("--config", required=True)
    sp.add_argument("--port", type=int, default=None, help="override api.port")
    sp.add_argument("--log-level", default="INFO")
    sp.add_argument("--no-docs", action="store_true",
                    help="no /docs, /redoc, /openapi.json (overrides api.docs)")  # fmt: skip
    keys = sub.add_parser("keys", help="client/admin keys")
    ksub = keys.add_subparsers(dest="keys_cmd", required=True)
    kn = ksub.add_parser("new", help="generate a key and print its sha256")
    kn.add_argument("--name", default=None)
    kn.add_argument("--out", default=None, help="write the key to this new file (0600)")
    admin_cli.add_commands(sub)
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return _serve(args)
    if args.cmd == "keys":
        return _keys_new(args)
    return admin_cli.dispatch(args)


if __name__ == "__main__":
    sys.exit(main())
