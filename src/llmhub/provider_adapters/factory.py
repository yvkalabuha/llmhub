"""One factory for the provider adapters of `providers.enabled` (T-372 B3).

Used by the API process (`server.build_service`), the reaper and the operator CLI, so every
process builds the same adapters from the same config. The RunPod key is read lazily from
`LLMHUB_RUNPOD_API_KEY_FILE` on each call (never a plain env value, never stored).
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping

import httpx

from llmhub.clock import Clock
from llmhub.configuration.loader import ConfigError, load_provider_key
from llmhub.configuration.models import Config
from llmhub.provider_adapters.base import ProviderAdapter

MOCK_STORE_ENV = "LLMHUB_MOCK_STORE"
DEFAULT_DISK_GB = 40


def build_provider_adapters(
    cfg: Config,
    env: Mapping[str, str],
    clock: Clock,
    *,
    runpod_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, ProviderAdapter]:
    adapters: dict[str, ProviderAdapter] = {}
    for name in cfg.providers.enabled:
        if name == "mock":
            from llmhub.provider_adapters.mock import MockProvider
            from llmhub.provider_adapters.mock_store import MockStore

            store = env.get(MOCK_STORE_ENV) or str(cfg.data_dir / "mock_cloud.db")
            adapters[name] = MockProvider(MockStore(store), clock)
        elif name == "runpod":
            adapters[name] = build_runpod(cfg, env, clock, transport=runpod_transport)
        else:
            raise ConfigError(f"provider {name!r} has no adapter")
    return adapters


def build_runpod(
    cfg: Config,
    env: Mapping[str, str],
    clock: Clock,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderAdapter:
    from llmhub.provider_adapters.rate_limit import RestLimiter
    from llmhub.provider_adapters.runpod import DEFAULT_PORT, RunpodAdapter
    from llmhub.provider_adapters.runpod_api import RunpodApi

    rp = cfg.providers.runpod
    if rp is None:
        raise ConfigError("providers.runpod: section required for the runpod adapter")
    key = load_provider_key("runpod", env)
    pools = [p for p in cfg.pools.values() if p.provider == "runpod"]
    disk = max((p.disk_gb for p in pools), default=DEFAULT_DISK_GB)
    ports = {p.infer.effective_port for p in pools}
    if len(ports) > 1:
        raise ConfigError("pools: runpod pools must share one infer port")
    port = ports.pop() if ports else DEFAULT_PORT
    limiter = RestLimiter(clock.now, hold_s=rp.rest_hold_s,
                          post_budget_per_hour=rp.post_budget_per_hour)  # fmt: skip
    api = RunpodApi(key, transport=transport, timeout_s=rp.call_timeout_s, limiter=limiter)
    return RunpodAdapter(api, rp, clock, disk_gb_hint=disk, port=port)


async def aclose_adapters(adapters: Mapping[str, ProviderAdapter]) -> None:
    """Close HTTP clients of adapters that have one (best effort)."""
    for a in adapters.values():
        inner = getattr(a, "inner", a)
        api = getattr(inner, "api", None)
        closer = getattr(api, "aclose", None)
        if closer is not None:
            with contextlib.suppress(Exception):
                await closer()


__all__ = ["MOCK_STORE_ENV", "aclose_adapters", "build_provider_adapters", "build_runpod"]

