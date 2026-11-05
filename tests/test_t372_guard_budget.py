"""T-372 L2b (2026-11-05): the guard's REST budget (report-only kinds every N-th pass, timer
120 s) and HTTP 429 as its own STOP code 9."""

from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest

from test_t372_guard import OURS, OURS2, ROOT, FakeOpener, _auto, _before, _watch, env, g

__all__ = ["env"]  # the fixture is used by name


def _rest_paths(op: FakeOpener) -> list[str]:
    return [u.split("?", 1)[0].replace(g.REST, "") for m, u, _ in op.calls
            if u.startswith(g.REST) and m == "GET"]  # fmt: skip


class _Limited(FakeOpener):
    """REST answers 429 for the given paths (GraphQL keeps working, as live)."""

    def __init__(self, base: FakeOpener, paths: set[str]) -> None:
        self.__dict__.update(base.__dict__)
        self.limited = paths

    def open(self, req: Any, timeout: float = 0) -> Any:
        path = req.full_url.split("?", 1)[0].replace(g.REST, "")
        if req.full_url.startswith(g.REST) and path in self.limited:
            self.calls.append((req.get_method(), req.full_url, req.get_header("Authorization")))
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                         {"Retry-After": "900"}, None)  # type: ignore[arg-type]
        return super().open(req, timeout)


def test_extras_every_tenth_pass(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    before = _before(op, tmp)
    op.calls.clear()
    for _ in range(11):
        assert _watch(tmp, before) == 0
    paths = _rest_paths(op)
    assert paths.count("/pods") == 11 and paths.count("/networkvolumes") == 2
    assert paths.count("/templates") == 2 and paths.count("/endpoints") == 2
    assert len(paths) == 11 + 2 * 4  # 19 REST calls for 11 passes (was 55)


def test_new_volume_found_on_the_next_extras_pass(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    before = _before(op, tmp)
    assert _watch(tmp, before, "--extras-every", "3") == 0  # pass 0: extras read
    op.volumes.append({"id": "v1", "name": "x"})
    assert _watch(tmp, before, "--extras-every", "3") == 0  # skipped: not compared
    assert _watch(tmp, before, "--extras-every", "3") == 0
    assert _watch(tmp, before, "--extras-every", "3") == 5  # pass 3


def test_without_state_dir_every_pass_reads_extras(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    before = _before(op, tmp)
    op.calls.clear()
    for _ in range(2):
        assert g.main(["watch", "--key-file", str(tmp / "ro"), "--before", str(before),
                       "--once"]) == 0  # fmt: skip
    assert _rest_paths(op).count("/networkvolumes") == 2


def test_429_on_pods_is_exit_9(env: tuple[FakeOpener, Path],
                               monkeypatch: pytest.MonkeyPatch,
                               capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    op, tmp = env
    before = _before(op, tmp)
    monkeypatch.setattr(g, "OPENER", _Limited(op, {"/pods"}))
    assert _auto(tmp, before) == 9
    err = capsys.readouterr().err
    assert "HTTP 429" in err and "Retry-After 900" in err and "rpa_" not in err


def test_429_on_extras_still_checks_and_deletes_then_exit_9(
        env: tuple[FakeOpener, Path], monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    op, tmp = env
    before = _before(op, tmp)
    op.pod("n1", OURS)
    lim = _Limited(op, {"/templates"})
    monkeypatch.setattr(g, "OPENER", lim)
    assert _auto(tmp, before) == 9
    assert "STOP: RunPod REST rate limit" in capsys.readouterr().out
    lim.pod("n2", OURS2)  # pods>1: the threshold code wins, deletes still happen
    assert _auto(tmp, before) == 6 and sorted(lim.deletes()) == ["n1", "n2"]


def test_bad_budget_arguments_are_usage_errors(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    before = _before(op, tmp)
    assert _watch(tmp, before, "--extras-every", "0") == 2
    assert _watch(tmp, before, "--interval", "5") == 2


def test_timer_every_120_s_and_unit_knows_code_9() -> None:
    timer = (ROOT / "deploy" / "workstation" / "llmhub-runpod-guard.timer").read_text()
    assert "OnUnitActiveSec=120" in timer.splitlines()
    unit = (ROOT / "deploy" / "workstation" / "llmhub-runpod-guard.service").read_text()
    assert "9 = RunPod REST rate limit" in unit
    assert g.DEFAULT_INTERVAL_S == 120 and g.DEFAULT_EXTRAS_EVERY == 10
