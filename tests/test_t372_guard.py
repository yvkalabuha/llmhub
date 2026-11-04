"""T-372 F1: the independent workstation guard (stdlib, raw JSON, fake urllib opener)."""

from __future__ import annotations

import ast
import datetime as dt
import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any, Self

import pytest

from t372_scripts import load

ROOT = Path(__file__).resolve().parent.parent
g = load("runpod_guard")

RO = "rpa_READONLY" + "r" * 30  # fictitious
RW = "rpa_READWRITE" + "w" * 30
T = 1_793_750_400.0
OURS = "llmhub-0123456789ab-01234567"
OURS2 = "llmhub-0123456789ac-01234567"


def _iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Resp(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


class FakeOpener:
    def __init__(self) -> None:
        self.pods: list[dict[str, Any]] = []
        self.volumes: list[dict[str, Any]] = []
        self.templates: list[dict[str, Any]] = []
        self.balance = 25.0
        self.spend = 0.0
        self.calls: list[tuple[str, str, str]] = []
        self.override: dict[str, Any] = {}

    def pod(self, pid: str, name: str, *, cost: float = 0.225, age_s: float = 60,
            status: str = "RUNNING") -> None:  # fmt: skip
        self.pods.append({"id": pid, "name": name, "desiredStatus": status, "costPerHr": cost,
                          "createdAt": _iso(T - age_s), "env": {"SECRET": "x"}})  # fmt: skip

    def open(self, req: Any, timeout: float = 0) -> _Resp:
        url, method = req.full_url, req.get_method()
        self.calls.append((method, url, req.get_header("Authorization")))
        path = url.split("?", 1)[0].replace(g.REST, "")
        if path in self.override:
            body = self.override[path]
            return _Resp(body if isinstance(body, bytes) else json.dumps(body).encode())
        if url == g.GRAPHQL:
            return _Resp(json.dumps({"data": {"myself": {
                "clientBalance": self.balance, "currentSpendPerHr": self.spend}}}).encode())
        if method == "DELETE":
            pid = path.rsplit("/", 1)[1]
            if not any(p["id"] == pid for p in self.pods):
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)  # type: ignore[arg-type]
            self.pods = [p for p in self.pods if p["id"] != pid]
            return _Resp(b"")
        data = {"/pods": self.pods, "/networkvolumes": self.volumes, "/templates": self.templates,
                "/containerregistryauth": [], "/endpoints": [],
                "/billing/pods": [{"amount": 0.1, "time": _iso(T), "podId": "p1",
                                   "secretField": "nope"}]}.get(path)  # fmt: skip
        if data is None:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)  # type: ignore[arg-type]
        return _Resp(json.dumps(data).encode())

    def deletes(self) -> list[str]:
        return [u.rsplit("/", 1)[1] for m, u, _ in self.calls if m == "DELETE"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeOpener, Path]:
    op = FakeOpener()
    monkeypatch.setattr(g, "OPENER", op)
    monkeypatch.setattr(g, "NOW", lambda: T)
    for name, key in (("ro", RO), ("rw", RW)):
        p = tmp_path / name
        p.write_text(key + "\n")
        p.chmod(0o600)
    return op, tmp_path


def _before(op: FakeOpener, tmp: Path) -> Path:
    out = tmp / "before.json"
    assert g.main(["snapshot", "--key-file", str(tmp / "ro"), "--out", str(out)]) == 0
    return out


def _watch(tmp: Path, before: Path, *extra: str) -> int:
    return g.main(["watch", "--key-file", str(tmp / "ro"), "--before", str(before), "--once",
                   "--state-dir", str(tmp / "state"), *extra])  # fmt: skip


def _auto(tmp: Path, before: Path) -> int:
    return _watch(tmp, before, "--auto-destroy", "--rw-key-file", str(tmp / "rw"))


def test_no_llmhub_imports() -> None:
    tree = ast.parse((ROOT / "scripts" / "runpod_guard.py").read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert "llmhub" not in mods and "httpx" not in mods
    assert mods <= set(sys.stdlib_module_names)


def test_snapshot_uses_bearer_and_keeps_no_env(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    op.pod("abc", "my-jupyter")
    snap = json.loads(_before(op, tmp).read_text())
    assert snap["pods"] == [{"id": "abc", "name": "my-jupyter", "status": "RUNNING",
                             "cost": 0.225, "created_at": T - 60}]  # fmt: skip
    assert all(h == f"Bearer {RO}" for _, _, h in op.calls)
    assert all(RO not in u for _, u, _ in op.calls)
    assert (tmp / "before.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("bad", [
    {"/pods": {"data": []}}, {"/pods": [{"id": "a", "name": "x", "desiredStatus": "RUNNING"}]},
    {"/pods": [{"id": "a", "name": "x", "desiredStatus": "RUNNING", "costPerHr": "0.2",
                "createdAt": "2026-11-04T00:00:00Z"}]},
    {"/pods": [{"id": "a", "name": "x", "desiredStatus": "RUNNING", "costPerHr": 0.2,
                "createdAt": "yesterday"}]},
    {"/networkvolumes": [{"name": "x"}]}, {"/pods": b"<html>"},
])  # fmt: skip
def test_unexpected_shape_is_exit_4(env: tuple[FakeOpener, Path], bad: dict[str, Any],
                                    capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    op, tmp = env
    op.override = bad
    assert g.main(["snapshot", "--key-file", str(tmp / "ro"), "--out", str(tmp / "s.json")]) == 4
    assert "parse error" in capsys.readouterr().err


def test_diff_cents_and_codes(env: tuple[FakeOpener, Path],
                              capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    op, tmp = env
    before = _before(op, tmp)
    op.pod("p1", OURS)
    op.balance = 24.87
    after = tmp / "after.json"
    g.main(["snapshot", "--key-file", str(tmp / "ro"), "--out", str(after)])
    capsys.readouterr()
    assert g.main(["diff", str(before), str(after)]) == 3
    out = capsys.readouterr().out
    assert f"new pods p1 {OURS}" in out and "balance_delta_cents=-13" in out
    op.templates.append({"id": "t1", "name": "llmhub-tpl"})
    g.main(["snapshot", "--key-file", str(tmp / "ro"), "--out", str(after)])
    assert g.main(["diff", str(before), str(after)]) == 5
    op.pods.clear()
    op.templates.clear()
    g.main(["snapshot", "--key-file", str(tmp / "ro"), "--out", str(after)])
    assert g.main(["diff", str(before), str(after)]) == 0


def test_auto_destroy_spares_foreign_old_and_young(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    op.pod("old", OURS2, age_s=9000)  # ours, but already in the "before" snapshot
    op.pod("mine", "my-own-pod", age_s=9000)
    before = _before(op, tmp)
    op.pods = [p for p in op.pods if p["id"] != "old"]
    op.pod("young", OURS, age_s=600)  # new, below every threshold
    op.pod("foreign-new", "someone-else", age_s=9000)
    assert _auto(tmp, before) == 0
    assert op.deletes() == []


@pytest.mark.parametrize("trigger", ["age", "spend", "count"])
def test_auto_destroy_new_pods_above_threshold(env: tuple[FakeOpener, Path], trigger: str
                                               ) -> None:  # fmt: skip
    op, tmp = env
    op.pod("keep", "my-own-pod", age_s=9000)
    before = _before(op, tmp)
    op.pod("new1", OURS, age_s=76 * 60 if trigger == "age" else 300)
    if trigger == "spend":
        op.spend = 0.31
    if trigger == "count":
        op.pod("new2", OURS2, age_s=100)
    op.pod("bad-name", "llmhub-not-a-label", age_s=9000)
    assert _auto(tmp, before) == 6
    want = ["new1"] + (["new2"] if trigger == "count" else [])
    assert sorted(op.deletes()) == sorted(want)
    assert [h for m, _, h in op.calls if m == "DELETE"] == [f"Bearer {RW}"] * len(want)
    log = (tmp / "state" / "actions.log").read_text()
    assert "destroy pod new1" in log and RW not in log and RO not in log


def test_threshold_without_auto_destroy_only_reports(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    before = _before(op, tmp)
    op.pod("new1", OURS, age_s=99 * 60)
    assert _watch(tmp, before) == 6 and op.deletes() == []


def test_volume_or_template_code_5_not_deleted(env: tuple[FakeOpener, Path],
                                               capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    op, tmp = env
    before = _before(op, tmp)
    op.volumes.append({"id": "v1", "name": "llmhub-vol"})
    assert _auto(tmp, before) == 5
    assert op.deletes() == [] and "reported, not deleted" in capsys.readouterr().out


def test_destroy_leftovers_dry_run_then_yes(env: tuple[FakeOpener, Path],
                                            capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    op, tmp = env
    op.pod("old", OURS2)
    before = _before(op, tmp)
    op.pod("left", OURS)
    op.pod("other", "my-pod")
    args = ["destroy-leftovers", "--key-file", str(tmp / "rw"), "--before", str(before)]
    assert g.main(args) == 0 and op.deletes() == []
    assert "would destroy pod left" in capsys.readouterr().out
    assert g.main([*args, "--yes"]) == 0
    assert op.deletes() == ["left"]


def test_keys_never_printed(env: tuple[FakeOpener, Path],
                            capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    op, tmp = env
    before = _before(op, tmp)
    op.pod("n", OURS, age_s=99 * 60)
    _auto(tmp, before)
    op.override = {"/pods": b"broken"}
    _auto(tmp, before)
    out, err = capsys.readouterr()
    for k in (RO, RW):
        assert k not in out + err
        for f in tmp.rglob("*"):
            if f.is_file() and f.name not in ("ro", "rw"):
                assert k not in f.read_text()


def test_key_file_mode_checked(env: tuple[FakeOpener, Path]) -> None:
    _, tmp = env
    (tmp / "ro").chmod(0o644)
    assert g.main(["snapshot", "--key-file", str(tmp / "ro"), "--out", str(tmp / "x")]) == 2


def test_record_allowlist(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    op.pod("p1", OURS)
    op.pods[0]["publicIp"] = "203.0.113.9"
    assert g.main(["record", "--key-file", str(tmp / "ro"), "--dir", str(tmp / "rec")]) == 0
    pods = json.loads((tmp / "rec" / "pods.json").read_text())
    assert pods["_source"] == "recorded" and pods["_kind"] == "pods"
    (item,) = pods["items"]
    assert "env" not in item and "publicIp" not in item and item["id"] == "p1"
    bill = json.loads((tmp / "rec" / "billing.json").read_text())
    assert "secretField" not in bill["items"][0]
