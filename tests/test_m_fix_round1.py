"""T-373 fix round 1: regression tests for SEC-1..3, low findings and C2-F1..F7."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp import Client

from api_fixtures import GAME_KEY, ApiEnv
from api_gateway_stubs import completion
from llmhub.mcp_server import checks as checks_mod
from llmhub.mcp_server import paths as paths_mod
from llmhub.mcp_server import service as service_mod
from llmhub.mcp_server.checks import CheckSpecError, build_checks, run_checks_async
from llmhub.mcp_server.client import HubClient, HubError
from llmhub.mcp_server.kinds import load_kinds
from llmhub.mcp_server.paths import PathDenied, Roots
from llmhub.mcp_server.sandbox import MAX_REGEX_TEXT, run_isolated, schema_outcome
from llmhub.mcp_server.secret import Secret
from llmhub.mcp_server.server import build_server
from llmhub.mcp_server.service import AskParams, McpService
from llmhub.mcp_server.settings import McpSettings
from m_fixtures import Hub, make_settings

EVIL = "regex:^(a+)+$"
EVIL_TEXT = "a" * 40 + "b"  # hours of backtracking


@pytest.fixture
async def hub(tmp_path: Path) -> AsyncIterator[Hub]:
    h = Hub(tmp_path)
    yield h
    await h.aclose()


def _children() -> list[int]:
    me = str(os.getpid())
    out: list[int] = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat_line = Path(f"/proc/{d}/stat").read_text()
            cmd = Path(f"/proc/{d}/cmdline").read_bytes()
        except OSError:
            continue
        ppid = stat_line.rsplit(")", 1)[1].split()[1]
        if ppid == me and b"llmhub.mcp_server.sandbox" in cmd:
            out.append(int(d))
    return out


# --- SEC-1: regex in a killable process -----------------------------------------------------
async def test_isolated_regex_matches_and_misses() -> None:
    cs = build_checks(["regex:^b+$", "no-regex:zzz"], None, kind_defaults=())
    assert all(c.isolated is not None for c in cs[1:])
    res = await run_checks_async("x\nbbb\n", "stop", cs)
    assert [(r.name, r.ok) for r in res] == [("not-truncated", True), ("regex", True),
                                             ("no-regex", True)]  # fmt: skip
    res = await run_checks_async("a zzz", "stop", cs)
    assert [(r.ok, r.reason) for r in res[1:]] == [(False, "not found"), (False, "found")]


async def test_catastrophic_regex_is_killed_and_loop_stays_free() -> None:
    cs = build_checks([EVIL], None, kind_defaults=())
    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    t = asyncio.create_task(ticker())
    start = time.monotonic()
    res = await run_checks_async(EVIL_TEXT, None, cs, timeout_s=1.0)
    elapsed = time.monotonic() - start
    t.cancel()
    assert res[-1].reason == "timeout" and not res[-1].ok
    assert elapsed < 2.0
    assert ticks >= 10, "the event loop must keep running while the regex runs"
    assert _children() == [], "the sandbox child must be killed and reaped"


async def test_one_budget_for_all_checks() -> None:
    cs = build_checks([EVIL, "no-regex:^(b+)+$", "max-bytes:1"], None, kind_defaults=())
    start = time.monotonic()
    res = await run_checks_async(EVIL_TEXT, None, cs, timeout_s=0.5)
    assert time.monotonic() - start < 1.5
    assert [r.reason for r in res[1:]] == ["timeout", "timeout", "timeout"]


async def test_regex_text_is_capped() -> None:
    cs = build_checks(["regex:a"], None, kind_defaults=())
    res = await run_checks_async("a" * (MAX_REGEX_TEXT + 1), None, cs)
    assert not res[-1].ok and "longer than" in res[-1].reason


def test_child_dies_on_its_own_alarm() -> None:
    """Orphan protection: the child arms SIGALRM, so it ends even if nobody kills it."""
    req = json.dumps({"spec": {"op": "regex", "pattern": "^(a+)+$", "negate": False},
                      "text": EVIL_TEXT, "alarm_s": 1})  # fmt: skip
    start = time.monotonic()
    p = subprocess.run([sys.executable, "-I", "-m", "llmhub.mcp_server.sandbox"],
                       input=req.encode(), capture_output=True, timeout=20, check=False)  # fmt: skip
    assert p.returncode == -signal.SIGALRM and time.monotonic() - start < 5


async def test_child_gets_no_llmhub_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLMHUB_KEY", "sk-should-not-leak")
    from llmhub.mcp_server import sandbox

    assert "LLMHUB_KEY" not in sandbox._child_env()  # pyright: ignore[reportPrivateUsage]
    assert await run_isolated({"op": "regex", "pattern": "x", "negate": False}, "x", 5) == (
        True, "")  # fmt: skip


async def test_schema_with_pattern_is_isolated_and_bounded() -> None:
    schema = {"type": "object", "properties": {"s": {"type": "string", "pattern": "^(a+)+$"}}}
    cs = build_checks([], schema, kind_defaults=())
    assert cs[1].name == "json-schema" and cs[1].isolated is not None
    start = time.monotonic()
    res = await run_checks_async(json.dumps({"s": EVIL_TEXT}), None, cs, timeout_s=0.8)
    assert res[1].reason == "timeout" and time.monotonic() - start < 2
    res = await run_checks_async(json.dumps({"s": "aaa"}), None, cs)
    assert res[1].ok


def test_schema_without_pattern_runs_inline() -> None:
    cs = build_checks([], {"type": "array"}, kind_defaults=())
    assert cs[1].isolated is None


async def test_server_stays_responsive_while_catastrophic_regex_runs(tmp_path: Path) -> None:
    (tmp_path / "hub").mkdir()
    env = ApiEnv(tmp_path / "hub")
    try:
        settings = make_settings(tmp_path, key_env=GAME_KEY, check_timeout_s=3.0)
        transport = httpx.ASGITransport(app=env.app, raise_app_exceptions=False)
        server = build_server(settings, transport=transport, sleep=env.pump,
                              monotonic=env.clock.now)  # fmt: skip
        env.worker.answer = completion(EVIL_TEXT)
        async with Client(server) as c:
            ask = asyncio.create_task(
                c.call_tool("llmhub_ask", {"input": "x", "checks": [EVIL], "wait_s": 9})
            )
            for _ in range(100):  # until the sandbox child runs the regex
                if _children():
                    break
                await asyncio.sleep(0.05)
            assert _children(), "the ask must reach the regex check"
            t0 = time.monotonic()
            await c.list_tools()  # protocol-level request (ping is removed in this mcp)
            models = await c.call_tool("llmhub_models", {})
            second = await c.call_tool("llmhub_ask", {"input": "x", "kind": "nope"})
            assert time.monotonic() - t0 < 1.0, "the server must stay responsive"
            assert not ask.done()
            assert models.structured_content and "kinds" in models.structured_content
            assert second.structured_content
            assert second.structured_content["error"]["code"] == "invalid_request"
            r = await ask
    finally:
        await env.aclose()
    sc = r.structured_content
    assert sc and sc["verdict"] == "check_failed" and sc["task_id"]
    assert [ch["reason"] for ch in sc["checks"] if ch["name"] == "regex"] == ["timeout"]


# --- low: json-schema reason length ---------------------------------------------------------
def test_schema_reason_is_length_capped() -> None:
    schema = {"type": "object", "additionalProperties": {"type": "object",
              "additionalProperties": {"type": "integer"}}}  # fmt: skip
    long_key = "k" * 500
    data = {long_key: {long_key + "2": "x", **{f"{i}": 1 for i in range(3)}}}
    ok, reason = schema_outcome(schema, json.dumps(data))
    assert not ok and len(reason) <= 200 + 2 + 40 and long_key not in reason


# --- C2-F2: references --------------------------------------------------------------------------
@pytest.mark.parametrize("schema", [
    {"$ref": "https://example.invalid/s.json"},
    {"$ref": "other.json#/x"},
    {"$ref": "#/$defs/missing"},
    {"$ref": "#nope"},
    {"$defs": {"a": {"$id": "urn:x", "type": "string"}}, "$ref": "#/$defs/a"},
    {"type": "object", "properties": {"a": {"$ref": "#/properties/b"}}},
])  # fmt: skip
def test_bad_references_refused(schema: dict[str, Any]) -> None:
    with pytest.raises(CheckSpecError):
        build_checks([], schema, kind_defaults=())


@pytest.mark.parametrize("schema", [
    {"$defs": {"n": {"type": "integer"}}, "type": "array", "items": {"$ref": "#/$defs/n"}},
    {"$defs": {"n": {"$anchor": "num", "type": "integer"}}, "items": {"$ref": "#num"}},
    {"type": "object", "properties": {"self": {"$ref": "#"}}},
    {"$defs": {"a~b/c": {"type": "integer"}}, "items": {"$ref": "#/$defs/a~0b~1c"}},
])  # fmt: skip
def test_local_references_accepted(schema: dict[str, Any]) -> None:
    c = build_checks([], schema, kind_defaults=())[1]
    assert c("[1]" if "items" in schema else "{}", "stop").ok


def test_schema_check_never_raises() -> None:
    assert schema_outcome({"$ref": "#/$defs/missing"}, "{}") == (False, "schema_error")


def test_deep_schema_refused() -> None:
    deep: dict[str, Any] = {}
    node = deep
    for _ in range(5000):
        node["items"] = {}
        node = node["items"]
    with pytest.raises(CheckSpecError):
        build_checks([], deep, kind_defaults=())


# --- C2-F1: task id survives every error after submit ---------------------------------------
async def test_exists_on_output_spills_to_scratch(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = hub.root / "late.txt"
    real = paths_mod.Roots.write_text

    def racing(self: Any, path: Path, text: str, *, overwrite: bool) -> None:
        if path == out.resolve():
            out.write_text("someone")
        real(self, path, text, overwrite=overwrite)

    monkeypatch.setattr(paths_mod.Roots, "write_text", racing)
    hub.worker.answer = completion("the paid answer")
    r = await hub.service.ask(AskParams(input="x", output_file=str(out), wait_s=9))
    assert r.verdict == "error" and r.error and r.error.code == "exists" and r.task_id
    assert out.read_text() == "someone"
    assert r.output_file and Path(r.output_file).parent == hub.roots.scratch
    assert Path(r.output_file).read_text() == "the paid answer"
    assert r.checks and all(ch.ok for ch in r.checks)


async def test_unexpected_error_after_submit_keeps_task_id(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(service_mod, "run_checks_async", boom)
    hub.worker.answer = completion("x")
    r = await hub.service.ask(AskParams(input="x", wait_s=9))
    assert r.error and r.error.code == "internal_error" and r.task_id
    r2 = await hub.service.result(r.task_id, wait_s=1)
    assert r2.error and r2.error.code == "internal_error" and r2.task_id == r.task_id


async def test_result_scratch_unavailable_keeps_task_id(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub.worker.answer = completion("y" * 20_000)  # > max_inline_bytes: spill to scratch
    p = await hub.service.ask(AskParams(input="x", wait_s=0))
    assert p.task_id

    def denied(self: Any, name: str) -> Path:
        raise PathDenied("scratch_unavailable")

    monkeypatch.setattr(paths_mod.Roots, "scratch_file", denied)
    r = await hub.service.result(p.task_id, wait_s=9)
    assert r.error and r.error.code == "scratch_unavailable" and r.task_id == p.task_id


# --- C2-F4: deadline in the client ------------------------------------------------------------
async def test_request_timeout_is_clamped_to_the_deadline() -> None:
    seen: list[float] = []
    t = [100.0]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.extensions["timeout"]["read"])
        return httpx.Response(200, json={"id": "a", "status": "running"})

    client = HubClient("http://t", lambda: Secret(GAME_KEY), timeout_s=30,
                       transport=httpx.MockTransport(handler), monotonic=lambda: t[0])  # fmt: skip
    try:
        await client.status("a", deadline=107.5)
        await client.status("a")
        with pytest.raises(HubError) as exc:
            await client.status("a", deadline=99.0)
    finally:
        await client.aclose()
    assert seen == [7.5, 30]
    assert exc.value.code == "deadline" and exc.value.retryable


def test_check_timeout_setting() -> None:
    assert McpSettings.from_env({}).check_timeout_s == 5.0
    assert McpSettings.from_env({"LLMHUB_MCP_CHECK_TIMEOUT_S": "2.5"}).check_timeout_s == 2.5
    with pytest.raises(ValueError):
        McpSettings.from_env({"LLMHUB_MCP_CHECK_TIMEOUT_S": "0"})


# --- C2-F5 / C2-F6: cancel hints, models robustness --------------------------------------------
def _mock_service(tmp_path: Path, handler: Any) -> tuple[McpService, HubClient]:
    settings = make_settings(tmp_path)
    client = HubClient("http://t", lambda: Secret(GAME_KEY), timeout_s=5,
                       transport=httpx.MockTransport(handler))  # fmt: skip
    roots = Roots.build(settings.roots, settings.scratch)
    return McpService(settings, client, roots, load_kinds(None)), client


@pytest.mark.parametrize(("status", "warning"), [
    ("running", "cancel_pending"), ("completed", "already_finished"), ("cancelled", None),
])  # fmt: skip
async def test_cancel_says_what_happened(tmp_path: Path, status: str, warning: str | None) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "t", "status": status,
                                         "cancel_requested": status == "running"})  # fmt: skip

    svc, client = _mock_service(tmp_path, handler)
    try:
        r = await svc.cancel("t")
    finally:
        await client.aclose()
    assert r.verdict == "ok" and r.status == status and r.hint
    assert r.warnings == ([warning] if warning else [])


async def test_models_tolerates_odd_meta_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m", "llmhub": "odd"}, {"id": 5}]})

    svc, client = _mock_service(tmp_path, handler)
    try:
        out = await svc.models()
        assert out["error"] is None
        assert out["models"] == [{"id": "m", "local": None, "cloud": None, "ctx_tokens": None},
                                 {"id": None, "local": None, "cloud": None,
                                  "ctx_tokens": None}]  # fmt: skip

        async def boom(**_k: Any) -> Any:
            raise RuntimeError("boom")

        monkeypatch.setattr(client, "models", boom)
        out = await svc.models()
    finally:
        await client.aclose()
    assert out["error"]["code"] == "internal_error" and out["models"] == []


# --- SEC-2: scratch directory -------------------------------------------------------------------
def test_scratch_symlink_uses_checked_target(tmp_path: Path) -> None:
    """A symlinked scratch is resolved once; the owner/mode checks apply to the target."""
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o777)
    link = tmp_path / "link"
    link.symlink_to(real)
    roots = Roots.build([], link)
    assert roots.scratch == real.resolve()
    assert roots.scratch_file("t.txt").parent == real.resolve()
    assert real.stat().st_mode & 0o777 == 0o700


def test_scratch_foreign_owner_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    uid = os.getuid()
    monkeypatch.setattr(paths_mod.os, "getuid", lambda: uid + 1)
    with caplog.at_level(logging.WARNING, "llmhub.mcp"):
        roots = Roots.build([], scratch)
    assert "not private" in caplog.text
    with pytest.raises(PathDenied) as exc:
        roots.scratch_file("t.txt")
    assert exc.value.code == "scratch_unavailable"


def test_new_scratch_is_0700(tmp_path: Path) -> None:
    roots = Roots.build([], tmp_path / "a" / "scratch")
    roots.scratch_file("t.txt")
    assert (tmp_path / "a" / "scratch").stat().st_mode & 0o777 == 0o700


# --- SEC-3: pseudo-filesystems and `/` --------------------------------------------------------
@pytest.mark.parametrize("path", ["/proc/self/environ", "/proc/1/cmdline", "/sys/kernel/hostname",
                                  "/dev/null", "/proc/self/root/etc/hostname"])  # fmt: skip
def test_pseudo_fs_refused_with_root_slash(tmp_path: Path, path: str) -> None:
    roots = Roots.build([Path("/")], tmp_path / "scratch")
    with pytest.raises(PathDenied):
        roots.read_text(roots.resolve_input(path), 1000)


@pytest.mark.parametrize("root", ["/", "/proc", "/proc/self", "/sys", "/dev"])
def test_forbidden_roots_are_skipped(tmp_path: Path, root: str) -> None:
    roots = Roots.build([Path(root)], tmp_path / "scratch")
    assert Path(root) not in roots.roots and Path(root) not in roots.lexical
    (tmp_path / "f.txt").write_text("x")
    with pytest.raises(PathDenied) as exc:
        roots.resolve_input(str(tmp_path / "f.txt"))
    assert exc.value.code == "outside_roots"
    assert roots.scratch_file("ok.txt").parent == roots.scratch


def test_symlink_root_to_proc_skipped(tmp_path: Path) -> None:
    link = tmp_path / "p"
    link.symlink_to("/proc/self")
    roots = Roots.build([link], tmp_path / "scratch")
    with pytest.raises(PathDenied):
        roots.resolve_input(str(link / "environ"))


# --- low: stale temp files ------------------------------------------------------------------------
def test_stale_tmp_files_removed_on_start(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    old = scratch / ".llmhub-mcp-0123456789abcdef.tmp"
    young = scratch / ".llmhub-mcp-fedcba9876543210.tmp"
    other = scratch / ".llmhub-mcp-notours.tmp"
    for f in (old, young, other):
        f.write_text("x")
    past = time.time() - 2 * paths_mod.STALE_TMP_S
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    Roots.build([], scratch)
    assert not old.exists() and young.exists() and other.exists()


def test_checks_module_has_no_inline_regex_search() -> None:
    """The parent never runs `re.search` on answer text (only `re.compile` to validate)."""
    src = Path(checks_mod.__file__).read_text()
    assert ".search(" not in src and ".match(" not in src
