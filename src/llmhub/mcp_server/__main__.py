"""`python -m llmhub.mcp_server`: stdio MCP server. stdout is the protocol channel; logs go to
stderr at WARNING (httpx logs URLs at INFO)."""

from __future__ import annotations

import logging
import os
import sys

from llmhub.mcp_server.kinds import KindsError
from llmhub.mcp_server.server import build_server
from llmhub.mcp_server.settings import McpSettings


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    for name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(name).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    try:
        settings = McpSettings.from_env(os.environ)
        server = build_server(settings)
    except (ValueError, KindsError) as exc:
        sys.stderr.write(f"llmhub-mcp: configuration error: {exc}\n")
        raise SystemExit(2) from None
    server.run("stdio")


if __name__ == "__main__":
    main()
