"""T-369 H2: LocalBackendClient against the ASGI fake."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fake_llama import FakeLlama
from llmhub.clock import FakeClock
from llmhub.configuration.api_models import LocalBackendConfig
from llmhub.local_inference.client import (
    LocalBackendClient,
    LocalError,
    api_key_from_env,
    build_backends,
)

CFG = LocalBackendConfig(
    url="http://fake-llama/v1", model="m-local", ctx_tokens=8192, pp_tps=100, tg_tps=5
)
REQ = {"messages": [{"role": "user", "content": "hi"}], "stream": True,
       "stream_options": {"include_usage": True}, "model": "client-says"}  # fmt: skip
KEY = "sk-very-secret-0123"


def mk(fake: FakeLlama, clock: FakeClock | None = None, key: str | None = None, cfg=CFG):  # type: ignore[no-untyped-def]
    return LocalBackendClient("cpu", cfg, key, http=fake.client(), clock=clock or FakeClock())


async def test_chat_ok_fields_and_model_substituted() -> None:
    fake = FakeLlama()
    r = await mk(fake).chat(REQ, timeout_s=10)
    assert (r.tokens_in, r.tokens_out, r.finish_reason) == (100, 40, "stop")
    assert (r.prompt_tps, r.gen_tps) == (50.0, 4.0)
    assert b"chat.completion" in r.body
    sent = fake.chat_requests[0].json
    assert sent["model"] == "m-local" and sent["stream"] is False
    assert "stream_options" not in sent


async def test_no_timings_gives_none() -> None:
    fake = FakeLlama()
    fake.timings = False
    r = await mk(fake).chat(REQ, timeout_s=10)
    assert r.prompt_tps is None and r.gen_tps is None


@pytest.mark.parametrize(
    ("action", "code"),
    [("context", "context_too_large"), (503, "local_unavailable"), (500, "local_unavailable"),
     ("drop", "local_unavailable"), (401, "local_unavailable"), (422, "bad_response")],
)  # fmt: skip
async def test_error_mapping(action: int | str, code: str) -> None:
    fake = FakeLlama()
    fake.chat_actions.append(action)
    with pytest.raises(LocalError) as ei:
        await mk(fake).chat(REQ, timeout_s=10)
    assert ei.value.code == code


async def test_non_json_is_bad_response() -> None:
    fake = FakeLlama()
    fake._chat_body = lambda req: {"nothing": 1}  # type: ignore[method-assign]
    with pytest.raises(LocalError) as ei:
        await mk(fake).chat(REQ, timeout_s=10)
    assert ei.value.code == "bad_response"


async def test_timeout_on_clock_and_upstream_aborted() -> None:
    clock = FakeClock()
    fake = FakeLlama(clock)
    fake.delays.append(500)
    task = asyncio.ensure_future(mk(fake, clock).chat(REQ, timeout_s=60))
    for _ in range(5):
        await asyncio.sleep(0)
    clock.advance_to(clock.now() + 61)
    with pytest.raises(LocalError) as ei:
        await task
    assert ei.value.code == "upstream_timeout"
    assert fake.aborted == 1


async def test_key_only_in_header_and_not_in_repr_or_errors() -> None:
    fake = FakeLlama(api_key=KEY)
    c = mk(fake, key=KEY)
    await c.chat(REQ, timeout_s=10)
    assert fake.chat_requests[0].headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in repr(c) and KEY not in str(vars(c.cfg))
    assert KEY not in str(fake.chat_requests[0].json)
    bad = mk(fake, key="wrong-key")
    with pytest.raises(LocalError) as ei:
        await bad.chat(REQ, timeout_s=10)
    assert "wrong-key" not in str(ei.value) and "wrong-key" not in repr(bad)
    fake.chat_actions.append("drop")
    with pytest.raises(LocalError) as ei2:
        await c.chat(REQ, timeout_s=10)
    assert KEY not in str(ei2.value) and ei2.value.__cause__ is None


def test_api_key_from_env() -> None:
    cfg = CFG.model_copy(update={"api_key_env": "LLAMA_KEY_X"})
    assert api_key_from_env(CFG, {}) is None
    assert api_key_from_env(cfg, {"LLAMA_KEY_X": "v"}) == "v"
    with pytest.raises(ValueError, match="LLAMA_KEY_X"):
        api_key_from_env(cfg, {})
    fake = FakeLlama()
    b = build_backends({"cpu": cfg}, FakeClock(), environ={"LLAMA_KEY_X": KEY}, http=fake.client())
    assert KEY not in repr(b["cpu"])


async def test_probe_slots_busy_and_idle() -> None:
    fake = FakeLlama()
    c = mk(fake)
    p = await c.probe()
    assert (p.ok, p.busy_slots, p.total_slots, p.status) == (True, 0, 1, 200)
    fake.external_busy = True
    p = await c.probe()
    assert p.busy_slots == 1 and p.ok
    assert fake.requests[-1].path == "/slots"


@pytest.mark.parametrize("status", [404, 501])
async def test_slots_off_falls_back_to_health_for_good(status: int) -> None:
    fake = FakeLlama()
    fake.slots_status = status
    c = mk(fake)
    p = await c.probe()
    assert p.ok and p.total_slots is None and p.status == 200
    assert c.probe_mode == "health"
    fake.requests.clear()
    await c.probe()
    assert [r.path for r in fake.requests] == ["/health"]  # no more /slots
    fake.health_status = 503
    p = await c.probe()
    assert not p.ok and p.status == 503


async def test_probe_config_health_and_down() -> None:
    fake = FakeLlama()
    c = mk(fake, cfg=CFG.model_copy(update={"probe": "health"}))
    assert (await c.probe()).ok
    assert [r.path for r in fake.requests] == ["/health"]
    fake.slots_status = 200
    c2 = mk(fake)
    fake.chat_actions.clear()

    async def down(*a: object, **k: object) -> None:
        raise httpx.ConnectError("down")

    c2._http.get = down  # type: ignore[method-assign,assignment]
    p = await c2.probe()
    assert not p.ok and p.status is None
