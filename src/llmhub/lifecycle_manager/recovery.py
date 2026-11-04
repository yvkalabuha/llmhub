"""Restart reconciliation. S0: records the new boot_id only; stream C/F fill in section 8."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field

from llmhub.cloud_manager.inference import InferenceBackend
from llmhub.configuration.models import Config
from llmhub.persistence.db import tx
from llmhub.provider_adapters.base import ProviderAdapter


@dataclass
class ReconcileReport:
    adopted: list[str] = field(default_factory=list)
    destroyed: list[str] = field(default_factory=list)
    errored: list[str] = field(default_factory=list)
    requeued_jobs: list[str] = field(default_factory=list)


async def reconcile_on_start(
    conn: sqlite3.Connection,
    adapters: Mapping[str, ProviderAdapter],
    inference: Mapping[str, InferenceBackend],
    secret: bytes,
    now: float,
    cfg: Config,
    boot_id: str,
) -> ReconcileReport:
    with tx(conn):
        conn.execute(
            "INSERT INTO controller_state(key, value, updated_at) VALUES ('boot_id', ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (boot_id, now),
        )
    return ReconcileReport()
