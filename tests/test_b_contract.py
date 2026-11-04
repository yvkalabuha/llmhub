import pytest

from b_fixtures import SECRET, MockHarness
from llmhub.provider_adapters.contract import CONTRACT_ITEMS, run_contract
from llmhub.provider_adapters.mock import MockKnobs, MockProvider


def test_items() -> None:
    assert CONTRACT_ITEMS == tuple(f"C{i}" for i in range(1, 11))


async def test_mock_passes_contract() -> None:
    await run_contract(MockHarness(), SECRET)


async def test_mock_passes_contract_with_lag() -> None:
    knobs = MockKnobs(destroy_delay_s=30, list_owned_lag_s=20)
    await run_contract(MockHarness(knobs), SECRET)


class WithStop(MockProvider):
    async def stop(self, ref: str) -> None:  # forbidden: only destroy ends billing
        return None


class NonIdempotentDestroy(MockProvider):
    async def destroy(self, ref):  # type: ignore[no-untyped-def]
        r = await super().destroy(ref)
        if r.value == "already_gone" and ref.startswith("mock-"):
            raise KeyError(ref)  # 404 not mapped to ALREADY_GONE
        return r


class LeakySecret(MockProvider):
    def normalize_error(self, exc):  # type: ignore[no-untyped-def]
        err = super().normalize_error(exc)
        from dataclasses import replace

        return replace(err, message=str(exc))


def _swap(cls: type[MockProvider]) -> MockHarness:
    h = MockHarness()
    h.adapter = h.provider = cls(h.provider.store, h.clock, h.knobs)
    return h


async def test_adapter_with_stop_fails_c1() -> None:
    with pytest.raises(AssertionError, match="^C1"):
        await run_contract(_swap(WithStop), SECRET)


async def test_non_idempotent_destroy_fails_c5() -> None:
    with pytest.raises(AssertionError, match="^C5"):
        await run_contract(_swap(NonIdempotentDestroy), SECRET)


async def test_secret_in_error_message_fails_c7() -> None:
    with pytest.raises(AssertionError, match="^C7"):
        await run_contract(_swap(LeakySecret), SECRET)
