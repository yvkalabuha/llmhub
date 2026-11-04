"""T-369 H1: the fake llama-server itself."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fake_llama import FakeLlama
from llmhub.clock import FakeClock

URL = "http://fake-llama/v1/chat/completions"
BODY = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}


async def test_chat_health_slots() -> None:
    fake = FakeLlama()
    async with fake.client() as http:
        r = await http.post(URL, json=BODY)
        d = r.json()
        assert r.status_code == 200 and d["usage"]["prompt_tokens"] == 100
        assert d["choices"][0]["finish_reason"] == "length"  # max_tokens 10 < 40
        assert d["timings"]["predicted_per_second"] == 4.0
        assert (await http.get("http://fake-llama/health")).status_code == 200
        fake.health_status = 503
        assert (await http.get("http://fake-llama/health")).status_code == 503
        assert (await http.get("http://fake-llama/slots")).json()[0]["is_processing"] is False
        fake.external_busy = True
        assert (await http.get("http://fake-llama/slots")).json()[0]["is_processing"] is True
        fake.slots_status = 501
        assert (await http.get("http://fake-llama/slots")).status_code == 501
    assert len(fake.chat_requests) == 1


async def test_auth_actions_and_log() -> None:
    fake = FakeLlama(api_key="k-test")
    async with fake.client() as http:
        assert (await http.post(URL, json=BODY)).status_code == 401
        h = {"Authorization": "Bearer k-test"}
        fake.chat_actions.extend([503, "context", "drop"])
        assert (await http.post(URL, json=BODY, headers=h)).status_code == 503
        r = await http.post(URL, json=BODY, headers=h)
        assert r.status_code == 400 and "context" in r.text
        with pytest.raises(httpx.RemoteProtocolError):
            await http.post(URL, json=BODY, headers=h)
        assert (await http.post(URL, json=BODY, headers=h)).status_code == 200


async def test_delay_on_clock_and_abort_seen() -> None:
    clock = FakeClock()
    fake = FakeLlama(clock)
    fake.delays.append(100)
    async with fake.client() as http:
        task = asyncio.ensure_future(http.post(URL, json=BODY))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done() and fake.in_flight == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert fake.aborted == 1 and fake.in_flight == 0
