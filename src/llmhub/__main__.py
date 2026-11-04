"""`python -m llmhub serve --config PATH` | `python -m llmhub keys new [--name N]`.

`serve` runs the API process (controller + local worker + gateway, one loop). The reaper is a
separate process: `python -m llmhub.reaper --config PATH`.
`keys new` prints a fresh client key ONCE (stdout) and its sha256 for `clients.<name>.key_sha256`
or `api.admin_key_sha256`; the key itself is never stored by llmhub.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path


def _serve(args: argparse.Namespace) -> int:
    from llmhub.api_gateway.logging import configure_logging
    from llmhub.clock import SystemClock
    from llmhub.configuration.loader import ConfigError, load_config
    from llmhub.server import build_service, serve

    configure_logging(args.log_level)
    path = Path(args.config)
    try:
        cfg = load_config(path)
        svc = build_service(cfg, os.environ, SystemClock(), config_path=path)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    asyncio.run(serve(svc, port=args.port))
    return 0


def _keys_new(args: argparse.Namespace) -> int:
    from llmhub.api_gateway.auth import generate_key, hash_key

    key = generate_key()
    print(f"key: {key}")
    print(f"key_sha256: {hash_key(key)}")
    if args.name:
        print(f'# config: clients.{args.name}.key_sha256: "{hash_key(key)}"')
    print("# the key is shown once; store it in the client's secret store", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m llmhub")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="run the API process")
    sp.add_argument("--config", required=True)
    sp.add_argument("--port", type=int, default=None, help="override api.port")
    sp.add_argument("--log-level", default="INFO")
    keys = sub.add_parser("keys", help="client/admin keys")
    ksub = keys.add_subparsers(dest="keys_cmd", required=True)
    kn = ksub.add_parser("new", help="generate a key and print its sha256")
    kn.add_argument("--name", default=None)
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        return _serve(args)
    return _keys_new(args)


if __name__ == "__main__":
    sys.exit(main())
