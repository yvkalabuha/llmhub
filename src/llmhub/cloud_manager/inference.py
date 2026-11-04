"""InferenceBackend protocol: how the core talks to the model server on an instance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class InstanceLost(Exception):
    """The resource disappeared (interruption, GPU loss) while a job was running."""


class NetworkLost(Exception):
    """The endpoint became unreachable while a job was running."""


@dataclass(frozen=True)
class JobRunSpec:
    job_id: str
    model_alias: str
    tokens_in: int
    max_tokens: int
    body: bytes


@dataclass(frozen=True)
class JobRunResult:
    tokens_in: int
    tokens_out: int
    finish_reason: str
    body: bytes


class InferenceBackend(Protocol):
    async def ping(self, endpoint: str) -> bool: ...
    async def health(self, endpoint: str) -> bool: ...
    async def test_request(self, endpoint: str) -> bool: ...
    async def run(self, endpoint: str, job: JobRunSpec) -> JobRunResult: ...
