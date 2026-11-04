"""Structural types of the neighbours the gateway talks to (LocalWorker, CloudState)."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Protocol

from llmhub.model_router.rules import CloudView
from llmhub.task_queue.models import Priority


class WorkerLike(Protocol):
    @property
    def stats(self) -> Mapping[str, Any]:
        """Backend name -> BackendStats (available, busy_external, pp, tg)."""
        ...

    def kick(self) -> None: ...
    def cancel(self, job_id: str) -> bool: ...
    def eta_s(
        self, alias: str, tokens_in: int, max_tokens: int, prio_rank: int, now: float
    ) -> float | None: ...
    def available(self, backend: str) -> bool: ...
    async def run_forever(self, stop: asyncio.Event) -> None: ...


class CloudLike(Protocol):
    def view(
        self, alias: str, tokens_in: int, max_tokens: int, priority: Priority
    ) -> CloudView: ...
