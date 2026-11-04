"""T-373 D2: McpService against the in-process gateway (ApiEnv, FakeClock, StubWorker)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from api_fixtures import PRIV_KEY
from api_gateway_stubs import completion
from llmhub.mcp_server.service import AskParams
from m_fixtures import Hub

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[Hub]:
    h = Hub(tmp_path)
    yield h
    await h.aclose()


def task_posts(hub: Hub) -> list[str]:
    return [r.headers["Idempotency-Key"] for r in hub.transport.requests
            if r.method == "POST" and r.url.path == "/v1/tasks"]  # fmt: skip


async def test_ok(hub: Hub) -> None:
    hub.worker.answer = completion('{"n": 3}')
    r = await hub.service.ask(AskParams(kind="extract_json", input="count", schema=SCHEMA,
                                        wait_s=30))  # fmt: skip
    assert r.verdict == "ok", r
    assert r.text == '{"n": 3}' and r.backend == "local:cpu" and r.status == "completed"
    assert r.tokens.prompt == 7 and r.tokens.completion == 3 and r.finish_reason == "stop"
    assert [c.name for c in r.checks] == ["not-truncated", "json-schema"]
    assert all(c.ok for c in r.checks) and r.error is None and r.task_id
    job = hub.env.job(r.task_id)
    assert job["data_class"] == "private"


async def test_private_header_on_every_request(hub: Hub) -> None:
    await hub.service.ask(AskParams(input="hi", wait_s=10))
    assert hub.transport.requests
    posts = [r for r in hub.transport.requests if r.method == "POST"]
    assert posts and all(r.headers["X-LLMHub-Data-Class"] == "private" for r in posts)


async def test_check_failed_goes_to_rejected_not_output(hub: Hub) -> None:
    hub.worker.answer = completion('{"n": "three"}')
    out = hub.root / "result.json"
    r = await hub.service.ask(AskParams(kind="extract_json", input="x", schema=SCHEMA,
                                        output_file=str(out), wait_s=30))  # fmt: skip
    assert r.verdict == "check_failed" and not out.exists()
    assert r.output_file is not None and r.output_file.endswith(f"{r.task_id}.rejected.txt")
    assert Path(r.output_file).read_text() == '{"n": "three"}'
    failed = [c for c in r.checks if not c.ok]
    assert [(c.name, c.reason) for c in failed] == [("json-schema", "/n: type")]
    assert r.hint and "yourself" in r.hint


async def test_ok_with_output_file(hub: Hub) -> None:
    out = hub.root / "result.txt"
    r = await hub.service.ask(AskParams(input="x", output_file=str(out), wait_s=30))
    assert r.verdict == "ok" and r.output_file == str(out.resolve())
    assert out.read_text() == "local answer" and r.text == "local answer"


async def test_pending_then_result(hub: Hub) -> None:
    hub.worker.hold = True
    r = await hub.service.ask(AskParams(input="later", wait_s=5))
    assert r.verdict == "pending" and r.task_id and r.status == "queued" and r.eta_s == 10.0
    assert r.hint and "llmhub_result" in r.hint
    again = await hub.service.ask(AskParams(input="later", wait_s=0))
    assert again.verdict == "pending" and again.task_id == r.task_id  # joined by the key
    keys = task_posts(hub)
    assert len(keys) == 2 and keys[0] == keys[1]
    hub.worker.hold = False
    done = await hub.service.result(r.task_id, wait_s=10)
    assert done.verdict == "ok" and done.text == "local answer"
    assert [c.name for c in done.checks] == ["not-truncated"]


async def test_result_without_wait_is_pending(hub: Hub) -> None:
    hub.worker.hold = True
    r = await hub.service.ask(AskParams(input="x", wait_s=0))
    assert (await hub.service.result(r.task_id or "")).verdict == "pending"


async def test_fresh_new_task(hub: Hub) -> None:
    a = await hub.service.ask(AskParams(input="same", wait_s=10))
    b = await hub.service.ask(AskParams(input="same", wait_s=10))
    c = await hub.service.ask(AskParams(input="same", wait_s=10, fresh=True))
    assert a.task_id == b.task_id and c.task_id != a.task_id
    assert a.verdict == b.verdict == c.verdict == "ok"


async def test_cloud_without_allow_is_local_refusal(hub: Hub) -> None:
    r = await hub.service.ask(AskParams(input="x", data_class="cloud_allowed"))
    assert r.verdict == "error" and r.error and r.error.code == "cloud_not_enabled"
    assert hub.transport.requests == []


async def test_cloud_allowed_but_client_private_stays_local(tmp_path: Path) -> None:
    hub = Hub(tmp_path, key=PRIV_KEY, allow_cloud=True)
    try:
        r = await hub.service.ask(AskParams(input="x", model="big", data_class="cloud_allowed",
                                            policy="QUALITY", wait_s=30))  # fmt: skip
        assert r.verdict == "ok" and r.backend == "local:cpu"
        post = next(q for q in hub.transport.requests if q.method == "POST")
        assert post.headers["X-LLMHub-Data-Class"] == "cloud_allowed"
        assert hub.env.job(r.task_id or "")["data_class"] == "private"
    finally:
        await hub.aclose()


async def test_length_is_check_failed(hub: Hub) -> None:
    hub.worker.answer = completion("cut", finish="length")
    r = await hub.service.ask(AskParams(input="x", wait_s=30))
    assert r.verdict == "check_failed" and r.truncated is True
    assert [(c.name, c.ok) for c in r.checks] == [("not-truncated", False)]


async def test_finish_error_is_upstream_rejected(hub: Hub) -> None:
    hub.worker.answer = completion("ctx too long", finish="error")
    r = await hub.service.ask(AskParams(input="x", wait_s=30))
    assert r.verdict == "error" and r.error and r.error.code == "upstream_rejected"
    assert r.task_id and r.status == "completed" and r.text is None


async def test_failed_task_is_error(hub: Hub) -> None:
    hub.worker.hold = True
    r = await hub.service.ask(AskParams(input="x", deadline_s=20, wait_s=60))
    assert r.verdict == "error" and r.status == "failed" and r.error
    assert r.error.code == "deadline_exceeded" and r.error.retryable is False
    assert r.error.reason is None or isinstance(r.error.reason, str)


async def test_cancel_then_result(hub: Hub) -> None:
    hub.worker.hold = True
    r = await hub.service.ask(AskParams(input="x", wait_s=0))
    c = await hub.service.cancel(r.task_id or "")
    assert c.verdict == "ok" and c.status == "cancelled"
    after = await hub.service.result(r.task_id or "")
    assert after.verdict == "error" and after.error and after.error.code == "cancelled"


async def test_unknown_task(hub: Hub) -> None:
    r = await hub.service.result("nope")
    assert r.verdict == "error" and r.error and r.error.code == "not_found"
    c = await hub.service.cancel("nope")
    assert c.verdict == "error" and c.error and c.error.code == "not_found"


async def test_input_files_content_in_body(hub: Hub) -> None:
    f = hub.root / "data.txt"
    f.write_text("FILE-CONTENT")
    await hub.service.ask(AskParams(input="use it", input_files=[str(f)], wait_s=10))
    post = next(q for q in hub.transport.requests if q.method == "POST")
    assert b"FILE-CONTENT" in post.content and str(hub.root).encode() not in post.content
    assert b"<<<FILE data.txt>>>" in post.content


async def test_same_path_new_content_new_task(hub: Hub) -> None:
    f = hub.root / "data.txt"
    f.write_text("v1")
    a = await hub.service.ask(AskParams(input="q", input_files=[str(f)], wait_s=10))
    f.write_text("v2")
    b = await hub.service.ask(AskParams(input="q", input_files=[str(f)], wait_s=10))
    assert a.task_id != b.task_id


async def test_models(hub: Hub) -> None:
    m = await hub.service.models()
    assert m["error"] is None
    ids = {x["id"] for x in m["models"]}
    assert {"auto", "big", "localonly"} <= ids
    assert {k["name"] for k in m["kinds"]} >= {"extract_json", "classify"}
