"""Parse and validate the weights plan (`LLMHUB_WEIGHTS_PLAN`). Never echoes plan values in errors."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_AUTH = ("weights_token", "none")
MAX_SEGMENTS = 16


class PlanError(ValueError):
    """Invalid plan. The message names the offending field, never its value."""


@dataclass(frozen=True)
class FileSpec:
    id: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SourceSpec:
    name: str
    base_url: str
    paths: Mapping[str, str]
    ca_pem: str | None
    auth: str
    min_speed_mb_s: float
    segments: int = 1


@dataclass(frozen=True)
class WeightsPlan:
    files: tuple[FileSpec, ...]
    sources: tuple[SourceSpec, ...]
    speed_grace_s: float = 30.0
    speed_window_s: float = 10.0
    read_timeout_s: float = 20.0
    connect_timeout_s: float = 10.0
    retries_per_source: int = 2
    deadline_unix: float | None = field(default=None)


def _num(obj: Mapping[str, Any], key: str, default: float, *, positive: bool = True) -> float:
    v = obj.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise PlanError(f"{key}: must be a number")
    if (positive and v <= 0) or v < 0:
        raise PlanError(f"{key}: out of range")
    return float(v)


def _int(obj: Mapping[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    v = obj.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise PlanError(f"{key}: must be an integer in [{lo}, {hi}]")
    return v


def _check_rel(path: object, where: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise PlanError(f"{where}: bad path")
    if any(c in path for c in "?#%\x00"):
        raise PlanError(f"{where}: bad path")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise PlanError(f"{where}: bad path")
    return path


def _source(raw: object, idx: int, ids: set[str]) -> SourceSpec:
    where = f"sources[{idx}]"
    if not isinstance(raw, dict):
        raise PlanError(f"{where}: must be an object")
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise PlanError(f"{where}.name: required")
    base = raw.get("base_url")
    if not isinstance(base, str):
        raise PlanError(f"{where}.base_url: required")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise PlanError(f"{where}.base_url: https url required")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise PlanError(f"{where}.base_url: credentials, query and fragment are not allowed")
    if not base.endswith("/"):
        base += "/"
    paths_raw = raw.get("paths")
    if not isinstance(paths_raw, dict):
        raise PlanError(f"{where}.paths: must be an object")
    paths: dict[str, str] = {}
    for fid in ids:
        if fid not in paths_raw:
            raise PlanError(f"{where}.paths: missing file id {fid}")
        paths[fid] = _check_rel(paths_raw[fid], f"{where}.paths.{fid}")
    ca = raw.get("ca_pem")
    if ca is not None and (not isinstance(ca, str) or "BEGIN CERTIFICATE" not in ca):
        raise PlanError(f"{where}.ca_pem: must be a PEM certificate or null")
    auth = raw.get("auth", "none")
    if auth not in _AUTH:
        raise PlanError(f"{where}.auth: unknown value")
    return SourceSpec(
        name=name,
        base_url=base,
        paths=paths,
        ca_pem=ca,
        auth=auth,
        min_speed_mb_s=_num(raw, "min_speed_mb_s", 0.0, positive=False),
        segments=_int(raw, "segments", 1, 1, MAX_SEGMENTS),
    )


def parse_plan(raw: str) -> WeightsPlan:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        raise PlanError("plan: not valid JSON") from None
    if not isinstance(data, dict):
        raise PlanError("plan: must be an object")
    if data.get("version") != 1:
        raise PlanError("plan: unsupported version")
    files_raw = data.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise PlanError("files: non-empty list required")
    files: list[FileSpec] = []
    for i, f in enumerate(files_raw):
        if not isinstance(f, dict):
            raise PlanError(f"files[{i}]: must be an object")
        fid, size, sha = f.get("id"), f.get("size"), f.get("sha256")
        if not isinstance(fid, str) or not _ID_RE.match(fid):
            raise PlanError(f"files[{i}].id: bad id")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise PlanError(f"files[{i}].size: positive integer required")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise PlanError(f"files[{i}].sha256: 64 lower-case hex required")
        if any(x.id == fid for x in files):
            raise PlanError(f"files[{i}].id: duplicate")
        files.append(FileSpec(fid, size, sha))
    if not any(f.id == "model" for f in files):
        raise PlanError("files: 'model' is required")
    ids = {f.id for f in files}
    sources_raw = data.get("sources")
    if not isinstance(sources_raw, list) or not sources_raw:
        raise PlanError("sources: non-empty list required")
    sources = tuple(_source(s, i, ids) for i, s in enumerate(sources_raw))
    if len({s.name for s in sources}) != len(sources):
        raise PlanError("sources: duplicate name")
    deadline = data.get("deadline_unix")
    if deadline is not None and (
        isinstance(deadline, bool) or not isinstance(deadline, int | float)
    ):
        raise PlanError("deadline_unix: must be a number")
    return WeightsPlan(
        files=tuple(files),
        sources=sources,
        speed_grace_s=_num(data, "speed_grace_s", 30.0, positive=False),
        speed_window_s=_num(data, "speed_window_s", 10.0),
        read_timeout_s=_num(data, "read_timeout_s", 20.0),
        connect_timeout_s=_num(data, "connect_timeout_s", 10.0),
        retries_per_source=_int(data, "retries_per_source", 2, 0, 10),
        deadline_unix=float(deadline) if deadline is not None else None,
    )
