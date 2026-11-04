"""T-372 review cycle 1 (money and security): the workstation guard against the REAL RunPod
pod shape (openapi 3.0.3 of rest.runpod.io/v1, P0): a pod has no `createdAt`.

xfail(strict=True) = a confirmed defect; the test turns green (and must lose the marker)
once the guard is fixed.
"""

from __future__ import annotations

import http.server
import io
import json
import threading
import urllib.error
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, Self

import pytest

from t372_scripts import load

g = load("runpod_guard")

ROOT = Path(__file__).resolve().parent.parent
OPENAPI = ROOT / "tests" / "fixtures" / "runpod" / "openapi.json"
RO = "rpa_READONLY" + "r" * 30  # fictitious
RW = "rpa_READWRITE" + "w" * 30
T = 1_793_750_400.0
OURS = "llmhub-0123456789ab-01234567"


def real_pod(pid: str, name: str, *, started_iso: str | None = "2026-11-04T10:00:00.000Z",
             cost: Any = 0.22) -> dict[str, Any]:  # fmt: skip
    """A pod with only fields of the real `Pod` schema (no `createdAt`)."""
    d: dict[str, Any] = {
        "id": pid, "name": name, "desiredStatus": "RUNNING", "costPerHr": cost,
        "adjustedCostPerHr": cost, "machineId": "m1", "image": "x@sha256:" + "0" * 64,
        "interruptible": False, "containerDiskInGb": 40, "volumeInGb": 0,
        "lastStatusChange": "Rented by User", "env": {"LLMHUB_API_KEY": "pod-secret"},
    }  # fmt: skip
    if started_iso is not None:
        d["lastStartedAt"] = started_iso
    return d


class _Resp(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        self.close()


class RealShapeOpener:
    def __init__(self) -> None:
        self.pods: list[dict[str, Any]] = []
        self.spend = 0.0
        self.deleted: list[str] = []

    def open(self, req: Any, timeout: float = 0) -> _Resp:
        url, method = req.full_url, req.get_method()
        if url == g.GRAPHQL:
            return _Resp(json.dumps({"data": {"myself": {
                "clientBalance": 19.85, "currentSpendPerHr": self.spend}}}).encode())
        path = url.split("?", 1)[0].replace(g.REST, "")
        if method == "DELETE":
            pid = path.rsplit("/", 1)[1]
            self.deleted.append(pid)
            self.pods = [p for p in self.pods if p["id"] != pid]
            return _Resp(b"")
        if path == "/pods":
            return _Resp(json.dumps(self.pods).encode())
        if path in ("/networkvolumes", "/templates", "/containerregistryauth", "/endpoints"):
            return _Resp(b"[]")
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)  # type: ignore[arg-type]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[RealShapeOpener, Path]:
    op = RealShapeOpener()
    monkeypatch.setattr(g, "OPENER", op)
    monkeypatch.setattr(g, "NOW", lambda: T)
    for name, key in (("ro", RO), ("rw", RW)):
        p = tmp_path / name
        p.write_text(key + "\n")
        p.chmod(0o600)
    return op, tmp_path


def test_real_openapi_pod_has_no_createdat() -> None:
    """The fact behind the findings below (skipped until the P0 file is present)."""
    if not OPENAPI.exists():
        pytest.skip("P0 openapi.json not downloaded")
    props = json.loads(OPENAPI.read_text())["components"]["schemas"]["Pod"]["properties"]
    assert "createdAt" not in props
    assert "lastStartedAt" in props and "costPerHr" in props


def test_snapshot_works_on_real_pod_shape(env: tuple[RealShapeOpener, Path]) -> None:
    op, tmp = env
    op.pods = [real_pod("p1", "someone-else")]
    assert g.main(["snapshot", "--key-file", str(tmp / "ro"), "--out",
                   str(tmp / "before.json")]) == 0  # fmt: skip


def test_destroy_leftovers_works_on_real_pod_shape(env: tuple[RealShapeOpener, Path]) -> None:
    op, tmp = env
    before = tmp / "before.json"
    before.write_text(json.dumps({"taken_at": T - 600, "balance": 19.85, "spend_per_hr": 0.0,
                                  "pods": [], "volumes": [], "templates": [],
                                  "registry_auths": [], "endpoints": []}))  # fmt: skip
    op.pods = [real_pod("p1", OURS), real_pod("p2", "someone-else")]
    assert g.main(["destroy-leftovers", "--key-file", str(tmp / "rw"), "--before", str(before),
                   "--yes"]) == 0  # fmt: skip
    assert op.deleted == ["p1"]  # ours only, never the foreign pod


def test_age_trigger_from_first_seen_persisted(env: tuple[RealShapeOpener, Path],
                                              monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    op, tmp = env
    before = tmp / "before.json"
    before.write_text(json.dumps({"taken_at": T - 600, "balance": 19.85, "spend_per_hr": 0.0,
                                  "pods": [], "volumes": [], "templates": [],
                                  "registry_auths": [], "endpoints": []}))  # fmt: skip
    op.pods = [real_pod("p1", OURS, started_iso=None)]
    args = ["watch", "--key-file", str(tmp / "ro"), "--before", str(before), "--once",
            "--auto-destroy", "--rw-key-file", str(tmp / "rw"), "--state-dir", str(tmp / "st")]
    assert g.main(args) == 0  # first sight: young
    monkeypatch.setattr(g, "NOW", lambda: T + 76 * 60)
    assert g.main(args) == 6 and op.deleted == ["p1"]


def test_candidates_never_foreign_or_preexisting() -> None:
    """Regression: only new pods with the strict llmhub label are candidates."""
    mk = [{"id": "a", "name": OURS, "status": "RUNNING", "cost": 0.2, "created_at": T},
          {"id": "b", "name": "llmhub-mine", "status": "RUNNING", "cost": 0.2, "created_at": T},
          {"id": "c", "name": "llmhub-0123456789ab-0123456", "status": "RUNNING", "cost": 0.2,
           "created_at": T},
          {"id": "d", "name": OURS, "status": "RUNNING", "cost": 0.2, "created_at": T}]  # fmt: skip
    before = {"pods": [mk[3]]}
    now = {"pods": mk}
    assert [p["id"] for p in g.candidates(before, now)] == ["a"]


# ------------------------------------------------------------------ redirect / key leak
class _Srv(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[str | None]] = []
    target: ClassVar[str] = ""

    def do_GET(self) -> None:
        if self.path.startswith("/redir"):
            self.send_response(302)
            self.send_header("Location", type(self).target + "/landing")
            self.end_headers()
            return
        type(self).seen.append(self.headers.get("Authorization"))
        body = b"[]"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def two_servers() -> Iterator[tuple[str, type[_Srv]]]:
    class A(_Srv):
        seen: ClassVar[list[str | None]] = []

    class B(_Srv):
        seen: ClassVar[list[str | None]] = []

    sa = http.server.HTTPServer(("127.0.0.1", 0), A)
    sb = http.server.HTTPServer(("127.0.0.1", 0), B)
    A.target = f"http://127.0.0.1:{sb.server_address[1]}"
    for s in (sa, sb):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{sa.server_address[1]}", B
    finally:
        for s in (sa, sb):
            s.shutdown()
            s.server_close()


def test_guard_does_not_forward_key_on_redirect(two_servers: tuple[str, type[_Srv]],
                                                monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    base, other = two_servers  # the module's own OPENER (no proxy env on this machine)
    for var in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    try:
        g.http_json("GET", base + "/redir", RO)
    except g.GuardError:
        pass  # refusing the redirect is the fix
    assert other.seen == [] or all(h is None for h in other.seen)
