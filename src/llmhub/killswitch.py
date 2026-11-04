"""Kill switch (stream F, step F7). S0 defines only the report type."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from llmhub.clock import Clock
from llmhub.configuration.models import Config
from llmhub.provider_adapters.base import ProviderAdapter


@dataclass
class KillReport:
    destroyed: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)


def check_kill_file(data_dir: Path) -> bool:
    return (data_dir / "KILL").exists()


async def kill_all(
    conn: sqlite3.Connection,
    adapters: Mapping[str, ProviderAdapter],
    secret: bytes,
    clock: Clock,
    cfg: Config,
    actor: Literal["controller", "reaper", "admin"],
    deadline_s: float = 60,
) -> KillReport:
    raise NotImplementedError("stream F (F7)")
