"""T-373 fix round 2: linear unfence (R3-3), trusted scratch symlinks (R3-1), whole-request
deadline (R3-2), sandbox lows (length check before spawn, minimal child env), kinds message."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import time
from pathlib import Path

import pytest

from llmhub.mcp_server import paths as paths_mod
from llmhub.mcp_server import sandbox
from llmhub.mcp_server.client import HubClient, HubError
from llmhub.mcp_server.kinds import KindsError, load_kinds
from llmhub.mcp_server.paths import Roots
from llmhub.mcp_server.secret import Secret

OLD_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.DOTALL)


def _old_unfence(text: str) -> str:
    m = OLD_FENCE.match(text)
    return m.group(1) if m else text


# --- R3-3 -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"a": 1}\n```',
        "```\n```",
        "```\n\n```",
        "```\n\n\n[1]\n\n```",
        "```\n  [1]\n```",
        "``` json\n[1]\n```",
        "```json {\n[1]\n```",
        "```json```",
        "```json\n[1]",
        "``````",
        "```\n[1]\n```\n```",
        "```\n[1]\n``` trailing",
        "  \n```py \t \n x \n\n```\n\n",
        "```a\r\n[1]\r\n```",
        "x```\n[1]\n```",
        "```é\n[1]\n```",
        "",
        "```",
        "```\n",
    ],
)
def test_unfence_matches_former_regex(text: str) -> None:
    assert sandbox.unfence(text) == _old_unfence(text)


def test_unfence_is_linear_on_a_fence_bomb() -> None:
    bomb = "```" + "\n" * 400_000 + "x"
    start = time.monotonic()
    assert sandbox.unfence(bomb) == bomb
    assert sandbox.unfence(bomb + "```") == "x"
    assert time.monotonic() - start < 0.5


def test_sandbox_module_has_no_regex_on_answer_text_in_the_parent() -> None:
    """Only `regex_outcome` (child side) searches; no module-level compiled pattern remains."""
    src = Path(sandbox.__file__).read_text()
    assert src.count("re.compile(") == 1 and "re.compile(pattern, re.MULTILINE).search" in src
    assert ".match(" not in src and "re.DOTALL" not in src


# --- R3-1 -----------------------------------------------------------------------------------
def _fake_owner(monkeypatch: pytest.MonkeyPatch, foreign: Path) -> None:
    real_lstat = os.lstat

    def fake_lstat(p: object, *a: object, **kw: object) -> os.stat_result:
        st = real_lstat(p, *a, **kw)  # type: ignore[arg-type]
        if os.fspath(p) == str(foreign):  # type: ignore[arg-type]
            vals = list(st)
            vals[stat.ST_UID] = os.getuid() + 1
            return os.stat_result(vals)
        return st

    monkeypatch.setattr(paths_mod.os, "lstat", fake_lstat)


def test_foreign_link_inside_own_link_target_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """own `a -> shared/b`, foreign `shared/b -> site`: realpath would follow both."""
    site = tmp_path / "site"
    site.mkdir()
    site.chmod(0o755)
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "b").symlink_to(site)
    (tmp_path / "a").symlink_to("shared/b")
    _fake_owner(monkeypatch, shared / "b")
    roots = Roots.build([], tmp_path / "a")
    assert stat.S_IMODE(site.stat().st_mode) == 0o755
    assert site.resolve() not in roots.roots
    assert (tmp_path / "a") not in roots.roots


def test_own_links_and_dotdot_are_trusted(tmp_path: Path) -> None:
    site = tmp_path / "d" / "site"
    site.mkdir(parents=True)
    (tmp_path / "up").symlink_to("d/site/..")
    assert paths_mod._links_trusted(str(tmp_path / "up" / "site" / "new"))  # pyright: ignore[reportPrivateUsage]
    roots = Roots.build([], tmp_path / "up" / "site")
    assert site.resolve() in roots.roots
    assert stat.S_IMODE(site.stat().st_mode) == 0o700


def test_symlink_loop_is_untrusted(tmp_path: Path) -> None:
    (tmp_path / "l1").symlink_to(tmp_path / "l2")
    (tmp_path / "l2").symlink_to(tmp_path / "l1")
    assert not paths_mod._links_trusted(str(tmp_path / "l1" / "x"))  # pyright: ignore[reportPrivateUsage]


def test_tightening_existing_scratch_mode_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    d = tmp_path / "scratch"
    d.mkdir()
    d.chmod(0o755)
    with caplog.at_level("WARNING", logger="llmhub.mcp"):
        Roots.build([], d)
    assert "tightened to 0700" in caplog.text
    assert stat.S_IMODE(d.stat().st_mode) == 0o700


# --- R3-2 -----------------------------------------------------------------------------------
async def test_request_without_deadline_is_bounded_by_timeout_s() -> None:
    """Even without a call deadline, a trickling response cannot outlive `timeout_s`."""

    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            await r.readuntil(b"\r\n\r\n")
            body = b'{"id":"t1","status":"running"}' + b" " * 40
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body))
            for b in body:
                w.write(bytes([b]))
                await w.drain()
                await asyncio.sleep(0.1)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    client = HubClient(f"http://127.0.0.1:{port}", lambda: Secret("k"), timeout_s=0.5)
    start = time.monotonic()
    try:
        with pytest.raises(HubError) as exc:
            await client.status("t1", budget_s=0.0)
        assert exc.value.code == "transport"
    finally:
        await client.aclose()
        srv.close()
    assert time.monotonic() - start < 3.0


# --- sandbox lows ---------------------------------------------------------------------------
async def test_long_text_regex_is_rejected_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_spawn(*_a: object, **_kw: object) -> None:
        raise AssertionError("child spawned")

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", no_spawn)
    text = "a" * (sandbox.MAX_REGEX_TEXT + 1)
    res = await sandbox.run_isolated({"op": "regex", "pattern": "a", "negate": False}, text, 5)
    assert res == (False, f"text longer than {sandbox.MAX_REGEX_TEXT} characters")


def test_child_env_is_minimal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("LANG", "C.UTF-8")
    env = sandbox._child_env()  # pyright: ignore[reportPrivateUsage]
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
    assert "PATH" in env and env["LANG"] == "C.UTF-8"


async def test_child_still_works_with_minimal_env() -> None:
    res = await sandbox.run_isolated({"op": "regex", "pattern": "ж", "negate": False}, "уж", 5)
    assert res == (True, "")


# --- kinds message --------------------------------------------------------------------------
def test_kinds_error_names_the_fix(tmp_path: Path) -> None:
    f = tmp_path / "kinds.yaml"
    f.write_text("classify:\n  max_tokens: 50\n")
    f.chmod(0o664)
    with pytest.raises(KindsError, match=r"\(chmod 644\)"):
        load_kinds(f)
