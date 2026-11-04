"""F8: the reaper as a separate process: `python -m llmhub.reaper --once` (marker slow)."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import TEST_SECRET
from llmhub.clock import SystemClock
from llmhub.persistence.db import connect, migrate
from llmhub.provider_adapters.labels import make_label, new_op_id
from llmhub.provider_adapters.mock import MockProvider
from llmhub.provider_adapters.mock_store import MockStore

pytestmark = pytest.mark.slow


def _setup(tmp_path: Path) -> tuple[Path, MockProvider]:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"data_dir: {tmp_path}\npools: {{}}\n", encoding="utf-8")
    conn = connect(tmp_path / "state.db")
    migrate(conn)
    conn.close()
    provider = MockProvider(MockStore(tmp_path / "mock_cloud.db"), SystemClock())
    return cfg, provider


def _run(cfg: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "LLMHUB_LABEL_SECRET": TEST_SECRET.decode(),
        "LLMHUB_MOCK_STORE": str(tmp_path / "mock_cloud.db"),
    }
    return subprocess.run(
        [sys.executable, "-m", "llmhub.reaper", "--config", str(cfg), "--once"],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def test_reaper_process_destroys_orphan_of_stopped_row(tmp_path: Path) -> None:
    cfg, provider = _setup(tmp_path)
    op = new_op_id()
    label = make_label(op, TEST_SECRET)
    conn = connect(tmp_path / "state.db")
    now = time.time()
    conn.execute(
        "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since, created_at,"
        " max_lifetime_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"inst-{op}", op, "p1", "mock", label, "STOPPED", now, now, now + 10800),
    )
    orphan = provider.add_orphan(label)
    foreign = provider.add_foreign("someone-else")
    out = _run(cfg, tmp_path)
    assert out.returncode == 0, out.stderr
    assert TEST_SECRET.decode() not in out.stdout + out.stderr
    assert provider.live_refs() == [foreign]
    assert orphan not in provider.live_refs()


def test_reaper_process_kill_file(tmp_path: Path) -> None:
    cfg, provider = _setup(tmp_path)
    ours = [provider.add_orphan(make_label(new_op_id(), TEST_SECRET)) for _ in range(3)]
    foreign = provider.add_foreign("someone-else")
    (tmp_path / "KILL").touch()
    started = time.monotonic()
    out = _run(cfg, tmp_path)
    assert out.returncode == 0, out.stderr
    assert time.monotonic() - started < 60
    assert provider.live_refs() == [foreign] and all(r not in provider.live_refs() for r in ours)
    conn = connect(tmp_path / "state.db")
    assert conn.execute("SELECT value FROM controller_state WHERE key='halted'").fetchone()[0] == "1"


def test_reaper_process_bad_config(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("pools: 3\n", encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "llmhub.reaper", "--config", str(cfg), "--once"],
        env={**os.environ, "LLMHUB_LABEL_SECRET": TEST_SECRET.decode()},
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert out.returncode == 2 and "config error" in out.stderr
