"""MockInference: simulated model server on top of MockProvider (stage 3a)."""

from __future__ import annotations

from dataclasses import dataclass

from llmhub.clock import Clock
from llmhub.cloud_manager.inference import InstanceLost, JobRunResult, JobRunSpec, NetworkLost
from llmhub.provider_adapters.base import RemoteStatus
from llmhub.provider_adapters.mock import MockProvider


@dataclass
class MockInferenceKnobs:
    load_s: float = 6
    pp_tps: float = 1200
    tg_tps: float = 78
    health_never: bool = False
    test_fail: int = 0  # number of test_request calls that fail first
    hang_load: bool = False


class MockInference:
    def __init__(
        self, provider: MockProvider, clock: Clock, knobs: MockInferenceKnobs | None = None
    ) -> None:
        self.provider = provider
        self.clock = clock
        self.knobs = knobs or MockInferenceKnobs()
        self._net_lost: dict[str, float] = {}
        self._test_calls: dict[str, int] = {}

    def lose_network(self, ref: str, until: float) -> None:
        self._net_lost[ref] = until

    @staticmethod
    def _ref(endpoint: str) -> str:
        return endpoint.removeprefix("mock://")

    async def _running_since(self, endpoint: str) -> float | None:
        res = await self.provider.get(self._ref(endpoint))
        if res is None or res.status != RemoteStatus.RUNNING:
            return None
        row = self.provider.store.conn.execute(
            "SELECT created_at, boot_s FROM resources WHERE ref=?", (res.ref,)
        ).fetchone()
        return float(row["created_at"] + row["boot_s"])

    def _reachable(self, endpoint: str) -> bool:
        return self.clock.now() >= self._net_lost.get(self._ref(endpoint), 0.0)

    async def ping(self, endpoint: str) -> bool:
        return self._reachable(endpoint) and await self._running_since(endpoint) is not None

    async def health(self, endpoint: str) -> bool:
        if self.knobs.health_never or self.knobs.hang_load or not self._reachable(endpoint):
            return False
        since = await self._running_since(endpoint)
        return since is not None and self.clock.now() >= since + self.knobs.load_s

    async def test_request(self, endpoint: str) -> bool:
        ref = self._ref(endpoint)
        n = self._test_calls.get(ref, 0)
        self._test_calls[ref] = n + 1
        if n < self.knobs.test_fail:
            return False
        return await self.health(endpoint)

    async def run(self, endpoint: str, job: JobRunSpec) -> JobRunResult:
        ref = self._ref(endpoint)
        duration = job.tokens_in / self.knobs.pp_tps + job.max_tokens / self.knobs.tg_tps
        await self.clock.sleep_until(self.clock.now() + duration)
        res = await self.provider.get(ref)
        if res is None or res.status != RemoteStatus.RUNNING:
            raise InstanceLost(ref)
        if not self._reachable(endpoint):
            raise NetworkLost(ref)
        return JobRunResult(
            tokens_in=job.tokens_in, tokens_out=job.max_tokens, finish_reason="stop", body=b"ok"
        )
