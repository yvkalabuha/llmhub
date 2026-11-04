"""Fixtures of stream B: mock provider environment and the contract harness."""

from __future__ import annotations

from pathlib import Path

from llmhub.clock import FakeClock
from llmhub.provider_adapters.base import InstanceSpec, OfferQuery
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.mock import Fault, MockKnobs, MockProvider
from llmhub.provider_adapters.mock_store import MockStore

SECRET = b"test-secret-0123456789abcdef-test-secret"  # fictitious


def spec(offer_id: str = "offer-0") -> InstanceSpec:
    return InstanceSpec(
        pool="p1", offer_id=offer_id, image="llmhub-infer:test", disk_gb=50,
        max_price_usd_h=0.5, max_lifetime_s=10800, env={},
    )  # fmt: skip


def label() -> str:
    return make_label(new_op_id(), SECRET)


def make_provider(
    knobs: MockKnobs | None = None, store: MockStore | None = None, clock: FakeClock | None = None
) -> tuple[MockProvider, FakeClock]:
    clk = clock or FakeClock()
    return MockProvider(store or MockStore(":memory:"), clk, knobs), clk


class MockHarness:
    """AdapterHarness for MockProvider."""

    def __init__(self, knobs: MockKnobs | None = None) -> None:
        self.knobs = knobs or MockKnobs()
        self.adapter, self.clock = make_provider(self.knobs)
        self.provider = self.adapter
        self.lagging = self.knobs.destroy_delay_s > 0 or self.knobs.list_owned_lag_s > 0

    async def settle(self) -> None:
        horizon = max(self.knobs.boot_s, 0.0) + self.knobs.destroy_delay_s
        horizon += self.knobs.list_owned_lag_s + self.knobs.late_visible_s + 1
        self.clock.advance_to(self.clock.now() + horizon)

    def make_spec(self) -> InstanceSpec:
        return spec()

    def add_foreign(self, label: str) -> str:
        return self.provider.add_foreign(label)

    def inject_unknown_create(self) -> None:
        self.provider.inject("create", Fault.CREATE_TIMEOUT_NOT_CREATED)


async def one_offer(provider: MockProvider) -> str:
    offers = await provider.list_offers(
        OfferQuery(gpu="RTX4090", min_vram_gb=24, max_price_usd_h=1.0, exclude_hosts=frozenset())
    )
    return offers[0].offer_id


def file_store(tmp_path: Path, name: str = "cloud.db") -> MockStore:
    return MockStore(tmp_path / name)
