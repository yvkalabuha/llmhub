"""YAML config loader and label-secret loader."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from llmhub.configuration.models import Config

SECRET_ENV = "LLMHUB_LABEL_SECRET"
INSTANCE_SECRET_ENV = "LLMHUB_INSTANCE_SECRET"
WEIGHTS_SECRET_ENV = "LLMHUB_WEIGHTS_SECRET"
SECRET_MIN_BYTES = 32


class ConfigError(Exception):
    """Invalid configuration; the message carries the field path, never a secret."""


def load_config(path: Path, env: Mapping[str, str] = os.environ) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read config {path}: {type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    pools = raw.get("pools")
    if isinstance(pools, dict):
        for name, p in pools.items():
            if isinstance(p, dict):
                p.setdefault("name", name)
    if "data_dir" not in raw and (d := env.get("LLMHUB_DATA_DIR")):
        raw["data_dir"] = d
    raw.setdefault("pools", {})
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        parts = [".".join(str(x) for x in e["loc"]) + ": " + e["msg"] for e in exc.errors()]
        raise ConfigError("; ".join(parts)) from None


FILE_SUFFIX = "_FILE"
_SECRET_FILE_MAX = 64 * 1024


def read_secret_file(path: Path) -> bytes:
    """First line of a secret file (T-371 §2.4, docker: ro mount, env holds only the path).

    The file must be a regular file (no symlink, FIFO or device; opened with O_NOFOLLOW and
    O_NONBLOCK) and not readable/writable by others. Errors never carry the content."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if Path(path).is_symlink():
            raise ConfigError(f"secret file {path}: must be a regular file") from None
        raise ConfigError(f"secret file {path}: cannot open ({type(exc).__name__})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"secret file {path}: must be a regular file")
        if st.st_mode & 0o007:
            raise ConfigError(f"secret file {path}: mode must not allow others (chmod 0640)")
        data = os.read(fd, _SECRET_FILE_MAX)
    finally:
        os.close(fd)
    return data.split(b"\n", 1)[0].strip()


def _load_secret(name: str, env: Mapping[str, str]) -> bytes:
    """`NAME` (value) or `NAME_FILE` (path to a file, preferred in containers); both -> error."""
    value = env.get(name)
    file = env.get(name + FILE_SUFFIX)
    if value is not None and file is not None:
        raise ConfigError(f"{name} and {name}{FILE_SUFFIX} are both set; use one")
    if file is not None:
        secret = read_secret_file(Path(file))
        if len(secret) < SECRET_MIN_BYTES:
            raise ConfigError(f"{name}{FILE_SUFFIX}: secret must be at least "
                              f"{SECRET_MIN_BYTES} bytes")  # fmt: skip
        return secret
    if value is None:
        raise ConfigError(f"{name} is not set (nor {name}{FILE_SUFFIX})")
    secret = value.encode()
    if len(secret) < SECRET_MIN_BYTES:
        raise ConfigError(f"{name} must be at least {SECRET_MIN_BYTES} bytes")
    return secret


def load_label_secret(env: Mapping[str, str] = os.environ) -> bytes:
    return _load_secret(SECRET_ENV, env)


def load_instance_secret(env: Mapping[str, str] = os.environ) -> bytes:
    """Derives per-instance API keys / TLS material (controller only)."""
    return _load_secret(INSTANCE_SECRET_ENV, env)


def load_weights_secret(env: Mapping[str, str] = os.environ) -> bytes:
    """Signs weights-server tokens (controller and weights validator)."""
    return _load_secret(WEIGHTS_SECRET_ENV, env)


def load_weights_ca(cfg: Config) -> dict[str, str]:
    """Source name -> PEM text of its pinned CA/certificate (sources with ca_pem_file)."""
    out: dict[str, str] = {}
    for src in cfg.weights.sources:
        if src.ca_pem_file is None:
            continue
        try:
            out[src.name] = Path(src.ca_pem_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"weights.sources.{src.name}.ca_pem_file: cannot read ({type(exc).__name__})"
            ) from exc
    return out
