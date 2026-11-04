"""T-373 D3/D4: McpService local refusals (no request sent), inline limit, writes, models."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from api_fixtures import GAME_KEY
from api_gateway_stubs import completion
from llmhub.mcp_server import paths as paths_mod
from llmhub.mcp_server.models import AskResult
from llmhub.mcp_server.secret import KeyUnavailable, Secret
from llmhub.mcp_server.service import AskParams
from m_fixtures import Hub


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[Hub]:
    h = Hub(tmp_path, max_input_bytes=100, max_inline_bytes=50, preview_bytes=11)
    yield h
    await h.aclose()


def code(r: AskResult) -> str:
    assert r.verdict == "error" and r.error is not None, r
    return r.error.code


async def refused(hub: Hub, p: AskParams) -> str:
    r = await hub.service.ask(p)
    assert hub.transport.requests == [], "a refused call must not reach llmhub"
    return code(r)


async def test_input_text_too_large(hub: Hub) -> None:
    assert await refused(hub, AskParams(input="x" * 101)) == "input_too_large"


async def test_input_files_sum_too_large(hub: Hub) -> None:
    a, b = hub.root / "a.txt", hub.root / "b.txt"
    a.write_text("a" * 60)
    b.write_text("b" * 30)
    ok = AskParams(input="x" * 10, input_files=[str(a), str(b)], wait_s=0)
    assert (await hub.service.ask(ok)).verdict != "error"
    hub.transport.requests.clear()
    p = AskParams(input="x" * 11, input_files=[str(a), str(b)])
    assert await refused(hub, p) == "input_too_large"


async def test_bad_output_refused_before_submit(hub: Hub) -> None:
    (hub.root / "exists.txt").write_text("old")
    assert await refused(hub, AskParams(input="x", output_file=str(hub.root / "exists.txt"))) \
        == "exists"  # fmt: skip
    assert await refused(hub, AskParams(input="x", output_file="/etc/out.txt")) == "outside_roots"
    assert await refused(hub, AskParams(input="x", output_file=str(hub.root / ".env"))) \
        == "dot_component"  # fmt: skip
    assert await refused(hub, AskParams(input="x", output_file="rel.txt")) == "path_not_absolute"


async def test_bad_input_file_refused(hub: Hub) -> None:
    assert await refused(hub, AskParams(input_files=["/etc/hostname"])) == "outside_roots"
    (hub.root / ".git").mkdir()
    (hub.root / ".git" / "config").write_text("x")
    p = AskParams(input_files=[str(hub.root / ".git" / "config")])
    assert await refused(hub, p) == "dot_component"


async def test_invalid_check_before_submit(hub: Hub) -> None:
    r = await hub.service.ask(AskParams(input="x", checks=["bogus"]))
    assert code(r) == "invalid_check" and hub.transport.requests == []


async def test_requires_schema(hub: Hub) -> None:
    assert await refused(hub, AskParams(kind="extract_json", input="x")) == "invalid_request"


async def test_unknown_kind_and_empty_input(hub: Hub) -> None:
    assert await refused(hub, AskParams(kind="nope", input="x")) == "invalid_request"
    assert await refused(hub, AskParams(input="  ")) == "invalid_request"
    assert await refused(hub, AskParams(input="x", max_tokens=0)) == "invalid_request"
    assert await refused(hub, AskParams(input="x", wait_s=-1)) == "invalid_request"


async def test_schema_from_file(hub: Hub) -> None:
    sf = hub.root / "schema.json"
    sf.write_text(json.dumps({"type": "array"}))
    hub.worker.answer = completion("[1]")
    r = await hub.service.ask(AskParams(kind="extract_json", input="x", schema=str(sf), wait_s=9))
    assert r.verdict == "ok" and [c.name for c in r.checks] == ["not-truncated", "json-schema"]
    body = json.loads(next(q for q in hub.transport.requests if q.method == "POST").content)
    assert body["response_format"]["json_schema"]["schema"] == {"type": "array"}


async def test_schema_file_refusals(hub: Hub) -> None:
    (hub.root / ".s.json").write_text("{}")
    p = AskParams(kind="extract_json", input="x", schema=str(hub.root / ".s.json"))
    assert await refused(hub, p) == "dot_component"
    (hub.root / "bad.json").write_text("not json")
    p = AskParams(kind="extract_json", input="x", schema=str(hub.root / "bad.json"))
    assert await refused(hub, p) == "invalid_request"


async def test_large_text_spills_to_scratch_with_utf8_preview(hub: Hub) -> None:
    text = "abcdefghij" + "я" * 40  # 10 + 80 bytes; preview limit 11 cuts inside "я"
    hub.worker.answer = completion(text)
    r = await hub.service.ask(AskParams(input="x", wait_s=9))
    assert r.verdict == "ok" and r.truncated_inline is True
    assert r.text == "abcdefghij" and len((r.text or "").encode()) <= 11
    assert r.output_file and Path(r.output_file).parent == hub.roots.scratch
    assert Path(r.output_file).name == f"{r.task_id}.txt"
    assert Path(r.output_file).read_text() == text


async def test_small_text_inline(hub: Hub) -> None:
    hub.worker.answer = completion("short")
    r = await hub.service.ask(AskParams(input="x", wait_s=9))
    assert r.text == "short" and r.output_file is None and r.truncated_inline is False


async def test_output_file_written_and_preview(hub: Hub) -> None:
    hub.worker.answer = completion("0123456789ABCDEF")
    out = hub.root / "o.txt"
    r = await hub.service.ask(AskParams(input="x", output_file=str(out), wait_s=9))
    assert r.verdict == "ok" and out.read_text() == "0123456789ABCDEF"
    assert r.text == "0123456789A" and r.truncated_inline is True


async def test_output_appeared_meanwhile_is_exists(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = hub.root / "late.txt"
    real = paths_mod.Roots.write_text

    def racing(self: Any, path: Path, text: str, *, overwrite: bool) -> None:
        if path == out.resolve():
            out.write_text("someone")
        real(self, path, text, overwrite=overwrite)

    monkeypatch.setattr(paths_mod.Roots, "write_text", racing)
    r = await hub.service.ask(AskParams(input="x", output_file=str(out), wait_s=9))
    assert code(r) == "exists" and out.read_text() == "someone"


async def test_wait_capped_by_max(tmp_path: Path) -> None:
    hub = Hub(tmp_path, max_wait_s=3)
    try:
        hub.worker.hold = True
        start = hub.env.clock.now()
        r = await hub.service.ask(AskParams(input="x", wait_s=1000))
        assert r.verdict == "pending" and hub.env.clock.now() - start <= 3.5
    finally:
        await hub.aclose()


async def test_no_url(tmp_path: Path) -> None:
    hub = Hub(tmp_path, url="")
    try:
        assert await refused(hub, AskParams(input="x")) == "config_missing"
        assert (await hub.service.models())["error"]["code"] == "config_missing"
    finally:
        await hub.aclose()


async def test_key_never_in_any_answer(hub: Hub) -> None:
    hub.worker.answer = completion("ok")
    answers = [
        await hub.service.ask(AskParams(input="x", wait_s=9)),
        await hub.service.ask(AskParams(input="x", checks=["regex:nomatch"], wait_s=9)),
        await hub.service.ask(AskParams(input="x", checks=["bad"])),
        await hub.service.result("nope"),
    ]
    for a in answers:
        dumped = a.model_dump_json()
        assert GAME_KEY not in dumped and "Bearer" not in dumped
    assert GAME_KEY not in json.dumps(await hub.service.models())


async def test_models_without_key(tmp_path: Path) -> None:
    def no_key() -> Secret:
        raise KeyUnavailable("missing")

    hub = Hub(tmp_path, key_fn=no_key)
    try:
        m = await hub.service.models()
        assert m["error"]["code"] == "key_unavailable" and m["error"]["reason"] == "missing"
        assert len(m["kinds"]) == 6 and m["models"] == []
        r = await hub.service.ask(AskParams(input="x"))
        assert code(r) == "key_unavailable"
    finally:
        await hub.aclose()


async def test_internal_error_has_no_trace(hub: Hub, monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("secret detail /etc/x")

    monkeypatch.setattr(hub.client, "submit", boom)
    r = await hub.service.ask(AskParams(input="x"))
    assert code(r) == "internal_error" and r.error and r.error.reason is None
    assert "secret detail" not in r.model_dump_json()


async def test_transport_error_after_submit_keeps_task_id(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from llmhub.mcp_server.client import HubError

    async def down(*a: Any, **k: Any) -> Any:
        raise HubError("transport", "ConnectError", retryable=True)

    hub.worker.hold = True
    monkeypatch.setattr(hub.client, "status", down)
    r = await hub.service.ask(AskParams(input="x", wait_s=5))
    assert code(r) == "transport" and r.task_id and r.error and r.error.retryable
    assert hub.env.job(r.task_id)["status"] == "queued"
    again = await hub.service.result(r.task_id)
    assert code(again) == "transport" and again.task_id == r.task_id
