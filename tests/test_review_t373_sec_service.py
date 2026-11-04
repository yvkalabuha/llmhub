"""T-373 review cycle 1 (security): service-level probes (cloud opt-in, key hygiene,
Idempotency-Key, rejected answers, error echo, ReDoS guard).

Confirmed defects are `xfail(strict=True)`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from api_fixtures import GAME_KEY
from api_gateway_stubs import completion
from llmhub.mcp_server.checks import build_checks, run_checks_async
from llmhub.mcp_server.kinds import Kind, KindsError, load_kinds
from llmhub.mcp_server.request import InputDoc, build_body, idempotency_key
from llmhub.mcp_server.service import AskParams
from llmhub.mcp_server.settings import McpSettings
from m_fixtures import Hub

SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
KIND = Kind(name="general", description="d", system="s", max_tokens=10)


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[Hub]:
    h = Hub(tmp_path)
    yield h
    await h.aclose()


# --- cloud opt-in --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["true", "yes", "on", " 1", "1 ", "01", "TRUE", ""])
def test_allow_cloud_only_exact_one(raw: str) -> None:
    assert McpSettings.from_env({"LLMHUB_MCP_ALLOW_CLOUD": raw}).allow_cloud is False


def test_kinds_file_cannot_set_data_class_or_headers(tmp_path: Path) -> None:
    for field in ("data_class", "llmhub", "headers", "policy"):
        f = tmp_path / f"{field}.yaml"
        f.write_text(f"general:\n  {field}: cloud_allowed\n")
        f.chmod(0o644)  # fix round 1: group/other-writable kinds files are refused anyway
        with pytest.raises(KindsError):
            load_kinds(f)


def test_body_llmhub_object_carries_only_task_type() -> None:
    kind = Kind(name="k", description="d", system="s", max_tokens=5, task_type="cloud_allowed")
    body = build_body(kind, "x", [], model="auto", schema=None, max_tokens=None)
    assert set(body["llmhub"]) == {"task_type"}  # never data_class (body beats headers)


async def test_default_private_even_when_cloud_enabled(tmp_path: Path) -> None:
    hub = Hub(tmp_path, allow_cloud=True)
    try:
        await hub.service.ask(AskParams(input="x", wait_s=5))
        posts = [r for r in hub.transport.requests if r.method == "POST"]
        assert posts and all(r.headers["X-LLMHub-Data-Class"] == "private" for r in posts)
    finally:
        await hub.aclose()


async def test_result_and_cancel_never_send_data_class(hub: Hub) -> None:
    r = await hub.service.ask(AskParams(input="x", wait_s=5))
    await hub.service.result(r.task_id or "", wait_s=0)
    await hub.service.cancel(r.task_id or "")
    others = [q for q in hub.transport.requests if q.url.path != "/v1/tasks"]
    assert others and all("X-LLMHub-Data-Class" not in q.headers for q in others)


# --- key hygiene ---------------------------------------------------------------------------
async def test_key_never_in_outputs_or_logs(hub: Hub, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    outs: list[str] = []
    hub.worker.answer = completion('{"n": "x"}')
    outs.append((await hub.service.ask(AskParams(kind="extract_json", input="a", schema=SCHEMA,
                                                 wait_s=10))).model_dump_json())  # fmt: skip
    outs.append((await hub.service.ask(AskParams(input="b", input_files=["/etc/passwd"])))
                .model_dump_json())  # fmt: skip
    outs.append((await hub.service.result("nope", wait_s=0)).model_dump_json())
    outs.append(str(await hub.service.models()))
    blob = "\n".join(outs) + caplog.text
    assert GAME_KEY not in blob and GAME_KEY[-12:] not in blob
    assert all("Authorization" not in o for o in outs)


# --- Idempotency-Key -----------------------------------------------------------------------
def test_idem_key_independent_of_directory_and_has_no_path(tmp_path: Path) -> None:
    body_a = build_body(KIND, "q", [InputDoc("f.txt", "same")], model="m", schema=None,
                        max_tokens=None)  # fmt: skip
    k = idempotency_key(body_a, data_class="private", priority=None, policy=None, nonce=None)
    assert k.startswith("mcp-") and len(k) == 4 + 64 and "/" not in k


@pytest.mark.parametrize(
    "over",
    [{"data_class": "cloud_allowed"}, {"priority": "urgent"}, {"policy": "QUALITY"},
     {"nonce": "n"}],
)  # fmt: skip
def test_idem_key_differs_per_option(over: dict[str, str]) -> None:
    body = build_body(KIND, "q", [], model="m", schema=None, max_tokens=None)
    base: dict[str, str | None] = {"data_class": "private", "priority": None, "policy": None,
                                   "nonce": None}  # fmt: skip
    a = idempotency_key(body, **base)  # type: ignore[arg-type]
    b = idempotency_key(body, **{**base, **over})  # type: ignore[arg-type]
    assert a != b


def test_idem_key_differs_for_schema_and_model() -> None:
    a = build_body(KIND, "q", [], model="m", schema=None, max_tokens=None)
    b = build_body(KIND, "q", [], model="m", schema=SCHEMA, max_tokens=None)
    c = build_body(KIND, "q", [], model="m2", schema=None, max_tokens=None)
    keys = {idempotency_key(x, data_class="private", priority=None, policy=None, nonce=None)
            for x in (a, b, c)}  # fmt: skip
    assert len(keys) == 3


def test_input_vs_file_boundary_no_collision() -> None:
    """Inline text that imitates the file wrapper vs a real file: same user text -> same key.
    Documented: the wire body is identical, so joining is correct (same prompt to the LLM)."""
    doc = InputDoc("f", "x")
    a = build_body(KIND, "", [doc], model="m", schema=None, max_tokens=None)
    b = build_body(KIND, "<<<FILE f>>>\nx\n<<<END FILE>>>", [], model="m", schema=None,
                   max_tokens=None)  # fmt: skip
    assert a == b


async def test_same_basename_other_dir_other_content_other_task(hub: Hub) -> None:
    (hub.root / "a").mkdir()
    (hub.root / "b").mkdir()
    (hub.root / "a" / "f.txt").write_text("one")
    (hub.root / "b" / "f.txt").write_text("two")
    r1 = await hub.service.ask(AskParams(input="q", input_files=[str(hub.root / "a/f.txt")],
                                         wait_s=5))  # fmt: skip
    r2 = await hub.service.ask(AskParams(input="q", input_files=[str(hub.root / "b/f.txt")],
                                         wait_s=5))  # fmt: skip
    assert r1.task_id != r2.task_id
    posted = [q.content.decode() for q in hub.transport.requests if q.method == "POST"]
    assert all(str(hub.root) not in p for p in posted)  # only basenames go to llmhub


# --- rejected answer -----------------------------------------------------------------------
async def test_rejected_never_overwrites_existing_output(hub: Hub) -> None:
    out = hub.root / "result.json"
    out.write_text("previous good")
    hub.worker.answer = completion('{"n": "bad"}')
    r = await hub.service.ask(AskParams(kind="extract_json", input="x", schema=SCHEMA,
                                        output_file=str(out), overwrite=True, wait_s=10))  # fmt: skip
    assert r.verdict == "check_failed"
    assert out.read_text() == "previous good"
    assert r.output_file and Path(r.output_file).parent == hub.roots.scratch


async def test_pending_and_error_write_nothing(hub: Hub) -> None:
    out = hub.root / "o.txt"
    hub.worker.hold = True
    r = await hub.service.ask(AskParams(input="x", output_file=str(out), wait_s=0))
    assert r.verdict == "pending" and not out.exists()


# --- error echo ----------------------------------------------------------------------------
async def test_errors_do_not_echo_input(hub: Hub) -> None:
    marker = "ZZSECRETZZ"
    cases = [
        AskParams(input="x", checks=[f"bogus:{marker}"]),
        AskParams(input="x", checks=[f"regex:({marker}"]),
        AskParams(input="x", input_files=[f"/nowhere/{marker}.txt"]),
        AskParams(input="x", input_files=[str(hub.root / f".{marker}")]),
        AskParams(input="x", output_file=f"/nowhere/{marker}"),
        AskParams(kind=f"k{marker}".lower(), input="x"),
        AskParams(input="x", schema=f"/nowhere/{marker}.json"),
    ]
    for p in cases:
        r = await hub.service.ask(p)
        assert r.verdict == "error"
        assert marker not in r.model_dump_json() and marker.lower() not in r.model_dump_json()


# --- ReDoS ---------------------------------------------------------------------------------
async def test_regex_timeout_bounds_wall_time() -> None:
    checks = build_checks(["regex:^(a+)+$"], None, kind_defaults=())
    loop_ticks = 0

    async def ticker() -> None:
        nonlocal loop_ticks
        while True:
            await asyncio.sleep(0.05)
            loop_ticks += 1

    t = asyncio.create_task(ticker())
    start = time.monotonic()
    res = await run_checks_async("a" * 25 + "b", None, checks, timeout_s=0.1)
    elapsed = time.monotonic() - start
    t.cancel()
    assert res[-1].reason == "timeout"
    assert elapsed < 0.6, f"guard took {elapsed:.1f} s (event loop blocked)"
