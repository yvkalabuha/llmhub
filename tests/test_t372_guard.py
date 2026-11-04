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
        self.delete_status: dict[str, int] = {}  # pod id -> HTTP status of its DELETE

    def pod(self, pid: str, name: str, *, cost: Any = 0.225, age_s: float | None = 60,
            status: str = "RUNNING") -> None:  # fmt: skip
        """Real `Pod` shape: no `createdAt`; `lastStartedAt` absent while the image is pulled."""
        d = {"id": pid, "name": name, "desiredStatus": status, "costPerHr": cost,
             "env": {"SECRET": "x"}}  # fmt: skip
        if age_s is not None:
            d["lastStartedAt"] = _iso(T - age_s)
        self.pods.append(d)

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
            if pid in self.delete_status:
                raise urllib.error.HTTPError(url, self.delete_status[pid], "x", {}, None)  # type: ignore[arg-type]
            if not any(p["id"] == pid for p in self.pods):
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)  # type: ignore[arg-type]
            self.pods = [p for p in self.pods if p["id"] != pid]
            return _Resp(b"")
        if method == "GET" and path.startswith("/pods/"):
            pid = path.rsplit("/", 1)[1]
            pod = next((p for p in self.pods if p["id"] == pid), None)
            if pod is None:
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)  # type: ignore[arg-type]
            return _Resp(json.dumps(pod).encode())
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
                             "cost": 0.225, "started_at": T - 60}]  # fmt: skip
    assert snap["errors"] == []
    assert all(h == f"Bearer {RO}" for _, _, h in op.calls)
    assert all(RO not in u for _, u, _ in op.calls)
    assert (tmp / "before.json").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("bad", [
    {"/pods": {"data": []}}, {"/pods": [{"id": "a", "name": "x"}]},
    {"/pods": [{"id": "a", "name": "x", "desiredStatus": "RUNNING", "costPerHr": "cheap"}]},
    {"/pods": [{"id": "a", "name": "x", "desiredStatus": "RUNNING", "costPerHr": 0.2,
                "lastStartedAt": "yesterday"}]},
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


# ------------------------------------------------------------------ T-372 fix round 1
@pytest.mark.parametrize("cost", ["0.22", 0.22, None])
def test_cost_number_string_or_absent(env: tuple[FakeOpener, Path], cost: Any) -> None:
    """REV-S5/R2-9: openapi `costPerHr` is a number with the example "0.74"."""
    op, tmp = env
    op.pod("p1", "x", cost=cost)
    snap = json.loads(_before(op, tmp).read_text())
    assert snap["pods"][0]["cost"] == (None if cost is None else 0.22)


def test_bad_optional_field_still_deletes_then_stops(env: tuple[FakeOpener, Path],
                                                     capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """REV-S1: a bad `costPerHr`/`lastStartedAt` never blocks the delete path; exit 4 after."""
    op, tmp = env
    before = _before(op, tmp)
    op.pod("n1", OURS, cost="??")
    op.pod("n2", OURS2, age_s=None)
    op.pods[1]["lastStartedAt"] = "not a time"
    assert _auto(tmp, before) == 6  # pods>1 fired; both deleted despite the bad fields
    assert sorted(op.deletes()) == ["n1", "n2"]
    assert "STOP: parse error: pod.costPerHr" in capsys.readouterr().out
    op.pod("n3", OURS, cost="??")  # no threshold: the parse error alone is a STOP
    assert _auto(tmp, before) == 4 and sorted(op.deletes()) == ["n1", "n2"]


def test_account_failure_is_soft(env: tuple[FakeOpener, Path]) -> None:
    """GraphQL down: the pod checks still run (pods>1 deletes), then STOP."""
    op, tmp = env
    before = _before(op, tmp)
    op.override = {g.GRAPHQL.replace(g.REST, ""): b"<html>"}
    op.pod("n1", OURS)
    assert _auto(tmp, before) == 4 and op.deletes() == []
    op.pod("n2", OURS2)
    assert _auto(tmp, before) == 6 and sorted(op.deletes()) == ["n1", "n2"]


def test_age_from_first_seen_survives_restart(env: tuple[FakeOpener, Path],
                                              monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """REV-S2: age = now - min(first seen, lastStartedAt); a restart resets lastStartedAt."""
    op, tmp = env
    before = _before(op, tmp)
    op.pod("n1", OURS, age_s=10)
    assert _auto(tmp, before) == 0
    seen = json.loads((tmp / "state" / "first_seen.json").read_text())
    assert seen == {"n1": T} and (tmp / "state" / "first_seen.json").stat().st_mode & 0o077 == 0
    monkeypatch.setattr(g, "NOW", lambda: T + 74 * 60)
    op.pods[0]["lastStartedAt"] = _iso(T + 73 * 60)  # restarted: lastStartedAt is young
    assert _auto(tmp, before) == 0
    monkeypatch.setattr(g, "NOW", lambda: T + 76 * 60)
    assert _auto(tmp, before) == 6 and op.deletes() == ["n1"]


def test_unreadable_state_resets_and_stops_after_checks(env: tuple[FakeOpener, Path],
                                                        capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    op, tmp = env
    before = _before(op, tmp)
    (tmp / "state").mkdir()
    (tmp / "state" / "first_seen.json").write_text("{broken")
    op.pod("n1", OURS)
    op.pod("n2", OURS2)
    assert _auto(tmp, before) == 6  # pods>1 still deleted
    assert sorted(op.deletes()) == ["n1", "n2"]
    assert "STOP: guard state reset" in capsys.readouterr().out
    (tmp / "state" / "first_seen.json").write_text("[1]")
    op.pod("n3", OURS)
    assert _auto(tmp, before) == 4  # nothing fired: the reset alone is a STOP
    assert json.loads((tmp / "state" / "first_seen.json").read_text()) == {"n3": T}


def test_auto_destroy_needs_state_dir(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    before = _before(op, tmp)
    assert g.main(["watch", "--key-file", str(tmp / "ro"), "--before", str(before), "--once",
                   "--auto-destroy", "--rw-key-file", str(tmp / "rw")]) == 2  # fmt: skip


def test_destroy_continues_after_a_failure(env: tuple[FakeOpener, Path],
                                           capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """REV-S8: one failed DELETE does not stop the others; all failures are reported."""
    op, tmp = env
    before = _before(op, tmp)
    op.pod("a1", OURS)
    op.pod("a2", OURS2)
    op.delete_status["a1"] = 500
    assert _auto(tmp, before) == 7
    assert op.deletes() == ["a1", "a2"] and [p["id"] for p in op.pods] == ["a1"]
    out = capsys.readouterr().out
    assert "destroy pod a1" in out and "FAILED" in out and "destroyed pod a2" in out
    args = ["destroy-leftovers", "--key-file", str(tmp / "rw"), "--before", str(before), "--yes"]
    op.pod("a3", OURS2)
    assert g.main(args) == 7 and op.deletes()[-2:] == ["a1", "a3"]


@pytest.mark.parametrize(("status", "exists", "code"), [(400, False, 0), (404, False, 0),
                                                        (400, True, 7)])  # fmt: skip
def test_delete_400_checked_with_get(env: tuple[FakeOpener, Path], status: int, exists: bool,
                                     code: int, capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """REV-S4/R2-7: DELETE -> 400 (documented) or 404 is gone only if GET /pods/{id} is 404."""
    op, tmp = env
    before = _before(op, tmp)
    op.pod("g1", OURS)
    op.delete_status["g1"] = status
    if not exists:
        op.pods.clear()
        op.override = {"/pods": [{"id": "g1", "name": OURS, "desiredStatus": "RUNNING"}]}
    args = ["destroy-leftovers", "--key-file", str(tmp / "rw"), "--before", str(before), "--yes"]
    assert g.main(args) == code
    if code == 0:
        assert "destroyed pod g1" in capsys.readouterr().out


def test_guard_expires(env: tuple[FakeOpener, Path], monkeypatch: pytest.MonkeyPatch,
                       capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """REV-S9b: after --until (default before.taken_at + 24 h) the guard only reports."""
    op, tmp = env
    before = _before(op, tmp)
    op.pod("n1", OURS, age_s=99 * 60)
    until = dt.datetime.fromtimestamp(T - 1, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert g.main(["watch", "--key-file", str(tmp / "ro"), "--before", str(before), "--once",
                   "--state-dir", str(tmp / "state"), "--auto-destroy", "--rw-key-file",
                   str(tmp / "rw"), "--until", until]) == 6  # fmt: skip
    assert op.deletes() == [] and "report only" in capsys.readouterr().out
    monkeypatch.setattr(g, "NOW", lambda: T + 24 * 3600 + 1)  # default expiry
    assert _auto(tmp, before) == 6 and op.deletes() == []
    monkeypatch.setattr(g, "NOW", lambda: T + 600)
    assert _auto(tmp, before) == 6 and op.deletes() == ["n1"]


def test_bad_until_is_usage_error(env: tuple[FakeOpener, Path]) -> None:
    op, tmp = env
    before = _before(op, tmp)
    assert _watch(tmp, before, "--until", "tomorrow") == 2


@pytest.mark.parametrize("content", [None, "{not json", '{"pods": []}'])
def test_watch_without_before_is_a_loud_stop(env: tuple[FakeOpener, Path], content: str | None,
                                             capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    """REV-S9a: a lost before.json must not switch auto-destroy off silently."""
    _, tmp = env
    before = tmp / "before.json"
    if content is not None:
        before.write_text(content)
    assert _auto(tmp, before) == 8
    out, err = capsys.readouterr()
    assert "STOP: no usable before snapshot" in out and "NOT watching" in err


def test_opener_has_no_proxy_and_no_redirect() -> None:
    """REV-S7: the module opener refuses redirects and ignores *_proxy variables."""
    handlers = g.OPENER.handlers
    assert any(isinstance(h, g._NoRedirect) for h in handlers)
    assert not any(type(h).__name__ == "HTTPRedirectHandler" for h in handlers)
    proxy = [h for h in handlers if type(h).__name__ == "ProxyHandler"]
    assert all(getattr(h, "proxies", None) == {} for h in proxy)  # no env proxy handler


def test_systemd_unit_sets_state_dir_and_expiry() -> None:
    """REV-S9: the timer's guard expires (GUARD_UNTIL = test end + 6 h) and keeps its state."""
    unit = (ROOT / "deploy" / "workstation" / "llmhub-runpod-guard.service").read_text()
    exec_start = next(x for x in unit.splitlines() if x.startswith("ExecStart="))
    assert "--auto-destroy" in exec_start and "--until ${GUARD_UNTIL}" in exec_start
    assert "--state-dir " in exec_start and "--before ${GUARD_BEFORE}" in exec_start
    env = (ROOT / "deploy" / "workstation" / "guard.env.example").read_text()
    assert "GUARD_UNTIL=" in env and "+ 6 h" in env
