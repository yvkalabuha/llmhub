"""MCP server settings read from the environment (T-373 plan §3.1)."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("llmhub.mcp")


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home())


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer") from None
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _float(env: Mapping[str, str], name: str, default: float, *, allow_zero: bool = False) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a finite non-negative number") from None
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} must be a finite non-negative number")
    return value


def _roots(raw: str) -> tuple[Path, ...]:
    out: list[Path] = []
    for part in raw.split(":"):
        if not part.strip():
            continue
        p = Path(part)
        if not p.is_dir():
            log.warning("LLMHUB_MCP_ROOTS: skipping a root that is not an existing directory")
            continue
        out.append(p)
    return tuple(out)


@dataclass(frozen=True)
class McpSettings:
    url: str
    key_env: str | None
    key_file: Path
    roots: tuple[Path, ...]
    scratch: Path
    kinds_file: Path | None
    allow_cloud: bool
    max_input_bytes: int = 400_000
    max_inline_bytes: int = 16_000
    preview_bytes: int = 2_000
    default_wait_s: float = 90.0
    max_wait_s: float = 300.0
    http_timeout_s: float = 30.0
    default_model: str = "auto"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> McpSettings:
        home = _home(env)
        key = (env.get("LLMHUB_KEY") or "").strip() or None
        kinds = (env.get("LLMHUB_MCP_KINDS") or "").strip()
        return cls(
            url=(env.get("LLMHUB_URL") or "").strip().rstrip("/"),
            key_env=key,
            key_file=Path(env.get("LLMHUB_KEY_FILE") or home / ".config/llmhub/key"),
            roots=_roots(env.get("LLMHUB_MCP_ROOTS", "")),
            scratch=Path(env.get("LLMHUB_MCP_SCRATCH") or home / ".local/state/llmhub-mcp/out"),
            kinds_file=Path(kinds) if kinds else None,
            allow_cloud=env.get("LLMHUB_MCP_ALLOW_CLOUD", "") == "1",
            max_input_bytes=_int(env, "LLMHUB_MCP_MAX_INPUT_BYTES", 400_000),
            max_inline_bytes=_int(env, "LLMHUB_MCP_MAX_INLINE_BYTES", 16_000),
            preview_bytes=_int(env, "LLMHUB_MCP_PREVIEW_BYTES", 2_000),
            default_wait_s=_float(env, "LLMHUB_MCP_DEFAULT_WAIT_S", 90.0, allow_zero=True),
            max_wait_s=_float(env, "LLMHUB_MCP_MAX_WAIT_S", 300.0, allow_zero=True),
            http_timeout_s=_float(env, "LLMHUB_MCP_HTTP_TIMEOUT_S", 30.0),
            default_model=(env.get("LLMHUB_MCP_MODEL") or "").strip() or "auto",
        )
