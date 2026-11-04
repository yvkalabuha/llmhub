"""YAML config loader and label-secret loader."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from llmhub.configuration.models import Config

SECRET_ENV = "LLMHUB_LABEL_SECRET"
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


def load_label_secret(env: Mapping[str, str] = os.environ) -> bytes:
    value = env.get(SECRET_ENV)
    if value is None:
        raise ConfigError(f"{SECRET_ENV} is not set")
    secret = value.encode()
    if len(secret) < SECRET_MIN_BYTES:
        raise ConfigError(f"{SECRET_ENV} must be at least {SECRET_MIN_BYTES} bytes")
    return secret
