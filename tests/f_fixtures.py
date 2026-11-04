"""Shared helpers for stream F scenario tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from harness import Harness, default_config
from llmhub.configuration.models import Config


def cfg_with(tmp_path: Path, pool: dict[str, Any] | None = None, **sections: Any) -> Config:
    """default_config with pool overrides and section overrides, e.g. timeouts={...}."""
    cfg = default_config(tmp_path, **(pool or {}))
    updates: dict[str, Any] = {}
    for name, values in sections.items():
        current = getattr(cfg, name)
        updates[name] = current.model_copy(update=values)
    return cfg.model_copy(update=updates)


def edges(h: Harness) -> list[tuple[str, str]]:
    return [(a, b) for a, b, _ in h.transitions()]


def job_states(h: Harness) -> list[tuple[str, str]]:
    return [(j["status"], j["route"]) for j in h.jobs()]


def count(h: Harness, sql: str, *params: Any) -> int:
    return int(h.conn.execute(sql, params).fetchone()[0])
