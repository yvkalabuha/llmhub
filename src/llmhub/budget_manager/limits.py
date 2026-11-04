"""Budget gates. S0 holds only the `Gate` type; stream E adds stages and check_launch."""

from __future__ import annotations

from dataclasses import dataclass

from llmhub.provider_adapters.base import UnavailableReason


@dataclass(frozen=True)
class Gate:
    ok: bool
    reason: UnavailableReason | None = None
    detail: str = ""
