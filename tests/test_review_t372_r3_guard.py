"""T-372 review cycle 3 (re-review of fix round 1, 0828b3d): the external guard end to end.

The confirmed defects (R3-1, R3-2) are fixed in fix round 2; markers removed.
"""

from __future__ import annotations

import io
import json
import subprocess
import textwrap
import urllib.error
from pathlib import Path
from typing import Any, Self

import pytest

from t372_scripts import SCRIPTS, load

g = load("runpod_guard")
UNIT_PYTHON = Path("/usr/bin/python3")  # ExecStart of deploy/workstation/llmhub-runpod-guard.service
OURS = "llmhub-0123456789ab-01234567"


def _pod(pid: str, name: str, status: str = "RUNNING", cost: Any = 0.25) -> dict[str, Any]:
    return {"id": pid, "name": name, "desiredStatus": status, "costPerHr": cost}


def _before(pods: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"taken_at": 1_000.0, "balance": 20.0, "spend_per_hr": 0.0,
            "pods": [{"id": p["id"], "name": p["name"], "status": p["desiredStatus"]}
                     for p in (pods or [])]}  # fmt: skip


class _Api:
    """Stub of `http_json`: GET /pods -> `pods`, GraphQL -> spend, DELETE recorded."""

    def __init__(self, pods: list[dict[str, Any]], spend: float = 0.0) -> None:
        self.pods = pods
        self.spend = spend
        self.deleted: list[str] = []

    def __call__(self, method: str, url: str, key: str, body: Any = None) -> Any:
        if "graphql" in url:
            return {"data": {"myself": {"clientBalance": 19.0, "currentSpendPerHr": self.spend}}}
        if method == "DELETE":
            self.deleted.append(url.rsplit("/", 1)[1])
            return None
        if url.endswith("/pods"):
            return self.pods
        return []


# ------------------------------------------------------------------ interpreter of the unit
def test_guard_watch_pass_runs_under_the_unit_interpreter(tmp_path: Path) -> None:
    if not UNIT_PYTHON.exists():
        pytest.skip("no /usr/bin/python3")
    harness = tmp_path / "h" / "run.py"
    harness.parent.mkdir()
    harness.write_text(textwrap.dedent(f"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("g", {str(SCRIPTS / "runpod_guard.py")!r})
        g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        pod = {{"id": "p1", "name": {OURS!r}, "desiredStatus": "RUNNING", "costPerHr": 0.25,
               "lastStartedAt": "2026-11-04 10:00:00"}}
        def fake(method, url, key, body=None):
            if "graphql" in url:
                return {{"data": {{"myself": {{"clientBalance": 19.0, "currentSpendPerHr": 0.9}}}}}}
            if url.endswith("/pods"):
                return [pod]
            return None if method == "DELETE" else []
        g.http_json = fake
        before = {{"taken_at": g.NOW(), "balance": 20.0, "pods": []}}
        print("code", g.watch_pass("k", "rw", before, {str(tmp_path / "st")!r}
                                   and __import__("pathlib").Path({str(tmp_path / "st")!r}),
                                   g.NOW() + 3600))
    """))
    out = subprocess.run([str(UNIT_PYTHON), "-I", str(harness)], capture_output=True,
                         text=True, timeout=60, check=False)  # fmt: skip
    assert out.returncode == 0, out.stderr[-400:]
    assert "code 6" in out.stdout and "destroyed pod p1" in out.stdout


# ------------------------------------------------------------------ DELETE 404 must be verified
class _Resp(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


def _opener(delete_status: int, get_status: int | None) -> Any:
    class O:
        def open(self, req: Any, timeout: float) -> Any:
            m = req.get_method()
            if m == "DELETE":
                raise urllib.error.HTTPError(req.full_url, delete_status, "x", {}, None)  # type: ignore[arg-type]
            if get_status is None:
                return _Resp(json.dumps(_pod("p1", OURS)).encode())
            raise urllib.error.HTTPError(req.full_url, get_status, "x", {}, None)  # type: ignore[arg-type]

    return O()


def test_guard_delete_404_with_live_pod_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "OPENER", _opener(404, None))
    with pytest.raises(g.GuardError) as e:
        g.delete_pod("p1", "rw")
    assert e.value.code == 7


def test_guard_delete_400_with_live_pod_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "OPENER", _opener(400, None))
    with pytest.raises(g.GuardError) as e:
        g.delete_pod("p1", "rw")
    assert e.value.code == 7 and e.value.status == 400


@pytest.mark.parametrize("get_status", [400, 401, 500])
def test_guard_delete_400_get_not_404_is_a_failure(monkeypatch: pytest.MonkeyPatch,
                                                   get_status: int) -> None:  # fmt: skip
    monkeypatch.setattr(g, "OPENER", _opener(400, get_status))
    with pytest.raises(g.GuardError):
        g.delete_pod("p1", "rw")


def test_guard_delete_400_get_404_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(g, "OPENER", _opener(400, 404))
    assert g.delete_pod("p1", "rw") == "gone"


# ------------------------------------------------------------------ never foreign / pre-existing
def test_watch_pass_spend_trigger_deletes_only_new_labelled(monkeypatch: pytest.MonkeyPatch,
                                                            tmp_path: Path) -> None:  # fmt: skip
    pre = _pod("old", "llmhub-aaaaaaaaaaaa-bbbbbbbb")
    pods = [pre, _pod("new", OURS), _pod("f1", "my-own-pod"), _pod("f2", "llmhub-manual"),
            _pod("t", "llmhub-cccccccccccc-dddddddd", "TERMINATED"),
            {"id": "x", "name": "llmhub-eeeeeeeeeeee-ffffffff", "desiredStatus": "EXITED",
             "costPerHr": "bad"}]  # fmt: skip
    api = _Api(pods, spend=0.9)
    monkeypatch.setattr(g, "http_json", api)
    monkeypatch.setattr(g, "NOW", lambda: 2_000.0)
    code = g.watch_pass("ro", "rw", _before([pre]), tmp_path, until=10_000.0)
    assert sorted(api.deleted) == ["new", "x"]  # EXITED still bills its disk: deleted too
    assert code == 6


def test_watch_pass_expired_guard_reports_only(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path, capsys: Any) -> None:  # fmt: skip
    api = _Api([_pod("new", OURS)], spend=0.9)
    monkeypatch.setattr(g, "http_json", api)
    monkeypatch.setattr(g, "NOW", lambda: 20_000.0)
    assert g.watch_pass("ro", "rw", _before(), tmp_path, until=10_000.0) == 6
    assert api.deleted == [] and "NOT deleted" in capsys.readouterr().out


def test_age_from_first_seen_without_last_started(monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:  # fmt: skip
    api = _Api([_pod("new", OURS)])
    monkeypatch.setattr(g, "http_json", api)
    t = [1_000.0]
    monkeypatch.setattr(g, "NOW", lambda: t[0])
    assert g.watch_pass("ro", "rw", _before(), tmp_path, until=1e12) == 0
    t[0] += g.MAX_AGE_S + 1
    assert g.watch_pass("ro", "rw", _before(), tmp_path, until=1e12) == 6
    assert api.deleted == ["new"]


def test_list_failure_is_a_loud_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise g.GuardError(7, "GET /pods -> HTTP 503", 503)

    monkeypatch.setattr(g, "http_json", boom)
    before = tmp_path / "before.json"
    before.write_text(json.dumps(_before()))
    for name in ("ro", "rw"):
        (tmp_path / name).write_text("rpa_x\n")
        (tmp_path / name).chmod(0o600)
    code = g.main(["watch", "--once", "--auto-destroy", "--key-file", str(tmp_path / "ro"),
                   "--rw-key-file", str(tmp_path / "rw"), "--before", str(before),
                   "--state-dir", str(tmp_path / "st")])  # fmt: skip
    assert code == 7


def test_missing_before_is_exit_8(tmp_path: Path) -> None:
    for name in ("ro", "rw"):
        (tmp_path / name).write_text("rpa_x\n")
        (tmp_path / name).chmod(0o600)
    code = g.main(["watch", "--once", "--auto-destroy", "--key-file", str(tmp_path / "ro"),
                   "--rw-key-file", str(tmp_path / "rw"), "--before", str(tmp_path / "nope"),
                   "--state-dir", str(tmp_path / "st")])  # fmt: skip
    assert code == 8
