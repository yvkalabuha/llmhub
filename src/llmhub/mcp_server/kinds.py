"""Task kinds: system prompt, token budget, thinking, default checks (T-373 plan §3.5)."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, replace
from importlib import resources
from pathlib import Path
from typing import Any, cast

import yaml

from llmhub.mcp_server.checks import CheckSpecError, parse_check

BUILTIN = "kinds.yaml"
_FIELDS = {"description", "system", "max_tokens", "think", "task_type", "checks",
           "requires_schema"}  # fmt: skip
_REQUIRED = ("description", "system", "max_tokens")
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class KindsError(ValueError):
    pass


@dataclass(frozen=True)
class Kind:
    name: str
    description: str
    system: str
    max_tokens: int
    think: bool | None = None
    task_type: str | None = None
    checks: tuple[str, ...] = ()
    requires_schema: bool = False


def _fields(name: str, raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise KindsError(f"kind {name}: expected a mapping")
    data = cast(dict[str, Any], raw)
    unknown = set(data) - _FIELDS
    if unknown:
        raise KindsError(f"kind {name}: unknown fields {sorted(unknown)}")
    out: dict[str, Any] = {}
    for key, value in data.items():
        if key in ("description", "system"):
            if not isinstance(value, str) or not value.strip():
                raise KindsError(f"kind {name}: {key} must be a non-empty string")
            out[key] = value.strip()
        elif key == "max_tokens":
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise KindsError(f"kind {name}: max_tokens must be a positive integer")
            out[key] = value
        elif key == "think":
            if value is not None and not isinstance(value, bool):
                raise KindsError(f"kind {name}: think must be true, false or null")
            out[key] = value
        elif key == "requires_schema":
            if not isinstance(value, bool):
                raise KindsError(f"kind {name}: requires_schema must be a boolean")
            out[key] = value
        elif key == "task_type":
            if value is not None and not isinstance(value, str):
                raise KindsError(f"kind {name}: task_type must be a string")
            out[key] = value
        elif key == "checks":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):  # pyright: ignore[reportUnknownVariableType]
                raise KindsError(f"kind {name}: checks must be a list of strings")
            specs = tuple(cast(list[str], value))
            for spec in specs:
                if spec == "json-schema":
                    continue
                try:
                    parse_check(spec)
                except CheckSpecError as exc:
                    raise KindsError(f"kind {name}: {exc}") from None
            out[key] = specs
    return out


def _merge(base: dict[str, Kind], text: str, source: str) -> dict[str, Kind]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        raise KindsError(f"{source}: not valid YAML") from None
    if doc is None:
        return base
    if not isinstance(doc, dict):
        raise KindsError(f"{source}: expected a mapping of kinds")
    out = dict(base)
    for name, raw in cast(dict[Any, Any], doc).items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise KindsError(f"{source}: bad kind name")
        fields = _fields(name, raw)
        if name in out:
            out[name] = replace(out[name], **fields)
            continue
        missing = [f for f in _REQUIRED if f not in fields]
        if missing:
            raise KindsError(f"kind {name}: missing {missing}")
        out[name] = Kind(name=name, **fields)
    return out


def load_kinds(user_file: Path | None) -> dict[str, Kind]:
    text = resources.files("llmhub.mcp_server").joinpath(BUILTIN).read_text(encoding="utf-8")
    kinds = _merge({}, text, BUILTIN)
    if user_file is None:
        return kinds
    return _merge(kinds, _read_user_file(user_file), "LLMHUB_MCP_KINDS")


def _read_user_file(path: Path) -> str:
    """The file sets system prompts and tool descriptions: only a regular file owned by the
    current user and not writable by group/others (review, low; same idea as the key file)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise KindsError("LLMHUB_MCP_KINDS: file is not readable") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise KindsError("LLMHUB_MCP_KINDS: not a regular file")
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise KindsError(
                "LLMHUB_MCP_KINDS: must be owned by the current user and not group/other-writable"
            )
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(1 << 20)
    except OSError:
        raise KindsError("LLMHUB_MCP_KINDS: file is not readable") from None
    finally:
        os.close(fd)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise KindsError("LLMHUB_MCP_KINDS: file is not readable") from None
