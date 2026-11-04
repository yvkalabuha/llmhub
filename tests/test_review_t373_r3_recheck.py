"""T-373 review cycle 3 (re-review of fix round 1): sandbox process hygiene, scratch symlink,
stale temp cleanup, call deadline vs slow responses, kinds-file mode under umask 002.

Confirmed defects R3-1..R3-3 were `xfail(strict=True)`; fixed in round 2, markers removed.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from llmhub.mcp_server import paths as paths_mod
from llmhub.mcp_server.checks import build_checks, run_checks_async
from llmhub.mcp_server.client import HubClient, HubError
from llmhub.mcp_server.kinds import KindsError, load_kinds
from llmhub.mcp_server.paths import Roots
from llmhub.mcp_server.sandbox import run_isolated
from llmhub.mcp_server.secret import Secret

EVIL_SPEC = {"op": "regex", "pattern": "^(a+)+$", "negate": False}
EVIL_TEXT = "a" * 40 + "b"
OK_SPEC = {"op": "regex", "pattern": "x", "negate": False}


def _sandbox_children() -> list[int]:
    me = str(os.getpid())
    out: list[int] = []
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            ppid = Path(f"/proc/{d}/stat").read_text().rsplit(")", 1)[1].split()[1]
            cmd = Path(f"/proc/{d}/cmdline").read_bytes()
        except OSError:
            continue
        if ppid == me and b"llmhub.mcp_server.sandbox" in cmd:
            out.append(int(d))
    return out


def _fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


# --- sandbox: process hygiene ---------------------------------------------------------------
async def test_many_calls_leak_no_fds_or_zombies() -> None:
    await run_isolated(OK_SPEC, "x", 5)  # warm up loop-level resources (child watcher, ...)
    before = _fd_count()
    for _ in range(30):
        assert await run_isolated(OK_SPEC, "x", 5) == (True, "")
    for _ in range(5):
        assert await run_isolated(EVIL_SPEC, EVIL_TEXT, 0.3) is None
    assert _fd_count() <= before
    assert _sandbox_children() == []


async def test_outer_cancellation_kills_and_reaps_child() -> None:
    task = asyncio.create_task(run_isolated(EVIL_SPEC, EVIL_TEXT, 30))
    for _ in range(100):
        await asyncio.sleep(0.05)
        if _sandbox_children():
            break
    assert _sandbox_children(), "child should be running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _sandbox_children() == []


async def test_child_environment_has_no_llmhub_or_python_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read the real environ of the running child, not only `_child_env()`."""
    monkeypatch.setenv("LLMHUB_KEY", "sk-r3-should-not-leak")
    monkeypatch.setenv("LLMHUB_KEY_FILE", "/nonexistent")
    monkeypatch.setenv("PYTHONSTARTUP", "/nonexistent/evil.py")
    task = asyncio.create_task(run_isolated(EVIL_SPEC, EVIL_TEXT, 3))
    env: list[bytes] = []
    for _ in range(100):
        await asyncio.sleep(0.05)
        kids = _sandbox_children()
        if kids:
            env = Path(f"/proc/{kids[0]}/environ").read_bytes().split(b"\0")
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env, "child environ was not observed"
    assert not [e for e in env if e.startswith((b"LLMHUB_", b"PYTHON"))]
    assert not any(b"sk-r3-should-not-leak" in e for e in env)


async def test_hostile_cwd_and_sys_path_are_not_imported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python -I`: a planted json.py / llmhub package in cwd or PYTHONPATH never runs."""
    hostile = tmp_path / "hostile"
    (hostile / "llmhub" / "mcp_server").mkdir(parents=True)
    marker = tmp_path / "PWNED"
    for rel in ("json.py", "re.py", "signal.py", "llmhub/__init__.py",
                "llmhub/mcp_server/__init__.py", "llmhub/mcp_server/sandbox.py"):  # fmt: skip
        (hostile / rel).write_text(f"open({str(marker)!r}, 'a').write({rel!r})\n")
    monkeypatch.chdir(hostile)
    monkeypatch.setenv("PYTHONPATH", str(hostile))
    monkeypatch.syspath_prepend(str(hostile))
    assert await run_isolated(OK_SPEC, "x", 10) == (True, "")
    assert not marker.exists()


async def test_large_text_goes_through_the_pipe_without_deadlock() -> None:
    """Text travels as JSON on stdin (no argv limit); ~3 M Cyrillic chars (~18 MB escaped)."""
    big = json.dumps({"k": "я" * 3_000_000})
    spec = {"op": "json-schema", "schema": {"properties": {"k": {"pattern": "^я+$"}}}}
    start = time.monotonic()
    assert await run_isolated(spec, big, 20) == (True, "")
    assert time.monotonic() - start < 10


async def test_spawn_cost_is_small() -> None:
    await run_isolated(OK_SPEC, "x", 5)
    start = time.monotonic()
    for _ in range(10):
        await run_isolated(OK_SPEC, "x", 5)
    per_call = (time.monotonic() - start) / 10
    assert per_call < 0.5, f"{per_call * 1000:.0f} ms per sandbox call"


async def test_regex_checks_share_budget_and_do_not_starve_later_checks() -> None:
    """After a killed regex the remaining non-regex checks get `timeout` (shared budget), never
    hang; total wall time stays near the budget."""
    cs = build_checks(["regex:^(a+)+$", "max-bytes:100"], None, kind_defaults=())
    start = time.monotonic()
    res = await run_checks_async(EVIL_TEXT, "stop", cs, timeout_s=0.5)
    assert time.monotonic() - start < 1.5
    assert [r.reason for r in res[1:]] == ["timeout", "timeout"]


# --- scratch: symlinks and stale temp files --------------------------------------------------
def test_scratch_symlink_not_owned_by_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLMHUB_MCP_SCRATCH in a shared dir (/tmp/x): another user plants x -> /home/me/site.
    `Roots.build` resolves it, lstat-s only the target (ours), chmods it 0700 and adds it to the
    allowed roots. Simulated: the link reports a foreign uid."""
    target = tmp_path / "site"
    target.mkdir()
    target.chmod(0o755)
    link = tmp_path / "x"
    link.symlink_to(target)
    real_lstat = os.lstat
    me = os.getuid()

    def fake_lstat(p: object, *a: object, **kw: object) -> os.stat_result:
        st = real_lstat(p, *a, **kw)  # type: ignore[arg-type]
        if os.fspath(p) == str(link):  # type: ignore[arg-type]
            vals = list(st)
            vals[stat.ST_UID] = me + 1
            return os.stat_result(vals)
        return st

    monkeypatch.setattr(paths_mod.os, "lstat", fake_lstat)
    roots = Roots.build([], link)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755, "foreign link: target must not change"
    assert target.resolve() not in roots.roots


def test_stale_cleanup_touches_only_own_regular_temp_files(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    outside = tmp_path / "precious.txt"
    outside.write_text("keep")
    past = time.time() - 2 * paths_mod.STALE_TMP_S
    os.utime(outside, (past, past))
    link = scratch / ".llmhub-mcp-00000000000000aa.tmp"  # name matches, but a symlink
    link.symlink_to(outside)
    sub = scratch / ".llmhub-mcp-00000000000000bb.tmp"  # name matches, but a directory
    sub.mkdir()
    os.utime(sub, (past, past))
    near = scratch / ".llmhub-mcp-00000000000000cc.tmpx"  # near-miss names
    near2 = scratch / "llmhub-mcp-00000000000000dd.tmp"
    upper = scratch / ".llmhub-mcp-00000000000000EE.tmp"
    user_file = scratch / "t1.txt"
    for f in (near, near2, upper, user_file):
        f.write_text("x")
        os.utime(f, (past, past))
    nested = sub / ".llmhub-mcp-00000000000000ff.tmp"  # only the top level is scanned
    nested.write_text("x")
    os.utime(nested, (past, past))
    Roots.build([], scratch)
    assert outside.read_text() == "keep" and os.path.islink(link)
    assert sub.is_dir() and nested.exists()
    assert near.exists() and near2.exists() and upper.exists() and user_file.exists()


def test_stale_cleanup_skips_foreign_owned_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    f = scratch / ".llmhub-mcp-0123456789abcdef.tmp"
    f.write_text("x")
    past = time.time() - 2 * paths_mod.STALE_TMP_S
    os.utime(f, (past, past))
    real_lstat = os.lstat
    me = os.getuid()

    def fake_lstat(p: object, *a: object, **kw: object) -> os.stat_result:
        st = real_lstat(p, *a, **kw)  # type: ignore[arg-type]
        if os.fspath(p) == str(f):  # type: ignore[arg-type]
            vals = list(st)
            vals[stat.ST_UID] = me + 1
            return os.stat_result(vals)
        return st

    monkeypatch.setattr(paths_mod.os, "lstat", fake_lstat)
    Roots.build([], scratch)
    assert f.exists()


# --- C2-F4: deadline vs a slowly trickling response -----------------------------------------
async def _drip_server() -> tuple[asyncio.Server, int]:
    async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            await r.readuntil(b"\r\n\r\n")
            body = b'{"id":"t1","status":"running"}' + b" " * 40
            w.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: %d\r\n\r\n" % len(body))  # fmt: skip
            await w.drain()
            for b in body:  # one byte per 0.1 s: each read is fast, the whole body is ~7 s
                w.write(bytes([b]))
                await w.drain()
                await asyncio.sleep(0.1)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            w.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


async def test_request_deadline_is_a_hard_wall_clock_bound() -> None:
    srv, port = await _drip_server()
    client = HubClient(f"http://127.0.0.1:{port}", lambda: Secret("k"), timeout_s=30)
    start = time.monotonic()
    try:
        with pytest.raises(HubError) as exc:
            await client.status("t1", deadline=time.monotonic() + 1.0)
        assert exc.value.code == "deadline"
    finally:
        elapsed = time.monotonic() - start
        await client.aclose()
        srv.close()
    assert elapsed < 2.0, f"request with a 1 s deadline took {elapsed:.1f} s"


# --- kinds file vs umask 002 -----------------------------------------------------------------
def test_kinds_file_created_under_umask_002_is_refused_with_clear_message(
    tmp_path: Path,
) -> None:
    """Documented trade-off: umask 002 gives 664 -> refused; the message names the rule, so the
    user can fix it (chmod 644/600). 600, 640, 644 are accepted."""
    f = tmp_path / "kinds.yaml"
    old = os.umask(0o002)
    try:
        f.write_text("classify:\n  max_tokens: 50\n")
    finally:
        os.umask(old)
    assert stat.S_IMODE(f.stat().st_mode) == 0o664
    with pytest.raises(KindsError, match="not group/other-writable"):
        load_kinds(f)
    for mode in (0o600, 0o640, 0o644, 0o444):
        f.chmod(mode)
        assert load_kinds(f)["classify"].max_tokens == 50


def test_sandbox_runs_with_the_server_interpreter() -> None:
    """The child uses sys.executable (the venv python), so -I still finds the installed package."""
    from llmhub.mcp_server import sandbox

    assert Path(sys.executable).exists()
    assert sandbox._MODULE == "llmhub.mcp_server.sandbox"  # pyright: ignore[reportPrivateUsage]


# --- residual of SEC-1: the fence regex runs in the parent ----------------------------------
FENCE_BOMB = "```" + "\n" * 20_000 + "x"  # quadratic in `_FENCE` (`\s*\n` x lazy `.*?`)


async def test_fence_unwrapping_does_not_freeze_the_loop() -> None:
    cs = build_checks(["item-count:1"], {"type": "object"}, kind_defaults=())
    assert all(c.isolated is None for c in cs), "these checks run in the parent"
    start = time.monotonic()
    res = await run_checks_async(FENCE_BOMB, "stop", cs, timeout_s=0.5)
    elapsed = time.monotonic() - start
    # wait_for cannot fire while `re` holds the GIL: elapsed > budget means a frozen loop
    assert elapsed < 1.5, f"checks took {elapsed:.1f} s with a 0.5 s budget"
    assert [r.name for r in res] == ["not-truncated", "json-schema", "item-count"]


def test_unfence_semantics_kept() -> None:
    """Pins the behaviour a linear rewrite of `unfence` must keep."""
    from llmhub.mcp_server.sandbox import load_json, unfence

    assert unfence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert unfence('  ```\n[1]\n```  \n') == "[1]"
    assert unfence("```js  \n[1]```") == "[1]"
    assert unfence("no fence") == "no fence"
    assert unfence("```\n[1]\n``` trailing") == "```\n[1]\n``` trailing"
    assert load_json('```json\n\n{"a": [1, 2]}\n\n```') == (True, {"a": [1, 2]})
