"""End-to-end check of a deployed llmhub on the Mock provider (T-371 §7).

    uv run python scripts/e2e_mock.py pre          --url http://10.8.0.1:8095 --key-file K \
        --admin-key-file A --state S.json
    (operator: docker restart llmhub-api)
    uv run python scripts/e2e_mock.py post-restart --url ... --key-file K --admin-key-file A \
        --state S.json
    uv run python scripts/e2e_mock.py kill         --url ... --key-file K --admin-key-file A

Phases:
- `pre`: /healthz, /v1/models, negatives (401 without key, 403 client key on /admin, /docs
  404), 429 on a cold pool (`--limit` low-priority cloud tasks queue for a batch, one more is
  refused), cloud chat (QUALITY, cloud_allowed) and the same as SSE stream up to [DONE], local
  chat (`max_tokens=8`), task with Idempotency-Key (poll, /result, repeat -> same id), lease
  create -> renew -> close, two long cloud tasks saved to `--state` for the restart.
- `post-restart`: the saved tasks still exist and reach `completed`; no orphan/unknown
  resource incidents.
- `kill`: client prewarm lease -> instance up -> admin kill (report `completed`, no
  `remaining`) -> 0 live and 0 STOPPING/PENDING_DELETE/LEAKED rows within 60 s, no
  `kill_incomplete`/`leak` incidents -> resume (an admin hold keeps a live GPU warm but never launches one).
- `prewarm` / `close-lease --lease ID`: S10w helper (a BOOTING Mock instance for the probe).

Keys are read from files (first line) and never printed; the report (stdout, JSON) carries
check names, HTTP statuses and timings only, never keys or texts. Exit 0 when every check
passed, 1 otherwise, 2 on bad arguments.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

CLOUD_HEADERS = {"X-LLMHub-Data-Class": "cloud_allowed", "X-LLMHub-Policy": "QUALITY"}


@dataclass
class Report:
    phase: str
    checks: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool, **detail: Any) -> bool:
        self.checks.append({"name": name, "ok": bool(ok), **detail})
        return ok

    @property
    def ok(self) -> bool:
        return all(c["ok"] for c in self.checks) and bool(self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "ok": self.ok, "checks": self.checks}


def read_key(path: str) -> str:
    line = Path(path).read_text(encoding="utf-8").split("\n", 1)[0].strip()
    if not line:
        raise ValueError(f"{path}: empty key file")
    return line


class Api:
    def __init__(self, url: str, key: str, admin_key: str | None, timeout: float) -> None:
        self.url = url.rstrip("/")
        self._key = key
        self._admin = admin_key
        self.timeout = timeout

    def _h(self, key: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"Authorization": f"Bearer {key}"} if key else {}
        return {**h, **(extra or {})}

    def req(
        self, method: str, path: str, *, key: str | None | bool = True,
        headers: dict[str, str] | None = None, body: Any = None, timeout: float | None = None,
    ) -> httpx.Response:  # fmt: skip
        k = self._key if key is True else (None if key is False else key)
        return httpx.request(method, self.url + path, headers=self._h(k, headers), json=body,
                             timeout=timeout or self.timeout)  # fmt: skip

    def admin(self, method: str, path: str, body: Any = None) -> httpx.Response:
        if self._admin is None:
            raise ValueError("--admin-key-file is required for this phase")
        return self.req(method, path, key=self._admin, body=body)

    def stream_done(self, path: str, body: Any, headers: dict[str, str]) -> tuple[int, bool]:
        with httpx.stream("POST", self.url + path, json=body, timeout=self.timeout,
                          headers=self._h(self._key, headers)) as r:  # fmt: skip
            done = False
            for line in r.iter_lines():
                if line.strip() == "data: [DONE]":
                    done = True
            return r.status_code, done


def chat(model: str, text: str, **extra: Any) -> dict[str, Any]:
    return {"model": model, "messages": [{"role": "user", "content": text}], **extra}


def wait_until(cond: Callable[[], bool], timeout: float, step: float = 1.0) -> float | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 <= timeout:
        if cond():
            return time.monotonic() - t0
        time.sleep(step)
    return None


def task_status(api: Api, task_id: str) -> str | None:
    r = api.req("GET", f"/v1/tasks/{task_id}")
    return str(r.json().get("status")) if r.status_code == 200 else None


# ------------------------------------------------------------------ phases
def phase_pre(api: Api, a: argparse.Namespace, rep: Report) -> None:
    r = api.req("GET", "/healthz", key=False)
    rep.add("healthz", r.status_code == 200, status=r.status_code)
    r = api.req("GET", "/v1/models")
    ids = [m.get("id") for m in r.json().get("data", [])] if r.status_code == 200 else []
    rep.add("models", r.status_code == 200 and a.cloud_model in ids, status=r.status_code)
    r = api.req("GET", "/v1/models", key=False)
    rep.add("negative_no_key_401", r.status_code == 401, status=r.status_code)
    r = api.req("GET", "/admin/status")
    rep.add("negative_client_key_admin_403", r.status_code == 403, status=r.status_code)
    r = api.req("GET", "/docs", key=False)
    rep.add("negative_docs_404", r.status_code == 404, status=r.status_code)

    # 429 on a cold pool: low-priority cloud tasks wait for a batch of 5 (no launch)
    low = {"llmhub": {"priority": "low"}}

    def submit(i: int) -> httpx.Response:
        return api.req("POST", "/v1/tasks", headers=CLOUD_HEADERS,
                       body=chat(a.cloud_model, f"e2e-limit-{i}-{uuid.uuid4().hex}", **low))  # fmt: skip

    with ThreadPoolExecutor(a.limit) as ex:
        first = list(ex.map(submit, range(a.limit)))
    extra = submit(a.limit)
    ok_first = all(x.status_code == 202 for x in first)
    rep.add("limit_429", ok_first and extra.status_code == 429
            and extra.headers.get("Retry-After") is not None,
            statuses=[x.status_code for x in first], extra=extra.status_code)  # fmt: skip
    for x in first:
        if x.status_code == 202:
            api.req("POST", f"/v1/tasks/{x.json()['id']}/cancel")

    t0 = time.monotonic()
    r = api.req("POST", "/v1/chat/completions", headers=CLOUD_HEADERS,
                body=chat(a.cloud_model, "e2e cloud", max_tokens=16), timeout=600)  # fmt: skip
    rep.add("cloud_chat", r.status_code == 200, status=r.status_code,
            backend=r.headers.get("X-LLMHub-Backend"), seconds=round(time.monotonic() - t0, 1))  # fmt: skip
    status, done = api.stream_done("/v1/chat/completions",
                                   chat(a.cloud_model, "e2e stream", max_tokens=16, stream=True),
                                   CLOUD_HEADERS)  # fmt: skip
    rep.add("cloud_stream_done", status == 200 and done, status=status)
    r = api.req("POST", "/v1/chat/completions", headers={"X-LLMHub-Data-Class": "private"},
                body=chat(a.local_model, "e2e local", max_tokens=8), timeout=600)  # fmt: skip
    rep.add("local_chat", r.status_code == 200, status=r.status_code,
            backend=r.headers.get("X-LLMHub-Backend"))  # fmt: skip

    idem = {"Idempotency-Key": f"e2e-{uuid.uuid4().hex}", **CLOUD_HEADERS}
    body = chat(a.cloud_model, "e2e task", max_tokens=16)
    r = api.req("POST", "/v1/tasks", headers=idem, body=body)
    tid = r.json().get("id") if r.status_code == 202 else None
    took = None if tid is None else wait_until(lambda: task_status(api, tid) == "completed",
                                               a.wait_s)  # fmt: skip
    res = api.req("GET", f"/v1/tasks/{tid}/result") if tid else None
    again = api.req("POST", "/v1/tasks", headers=idem, body=body)
    rep.add("task_idempotent", tid is not None and took is not None
            and res is not None and res.status_code == 200
            and again.status_code in (200, 202) and again.json().get("id") == tid,
            status=r.status_code, repeat=again.status_code)  # fmt: skip

    r = api.req("POST", "/v1/leases", body={"model": a.cloud_model, "ttl_s": 60})
    lid = r.json().get("id") if r.status_code == 201 else None
    rn = api.req("POST", f"/v1/leases/{lid}/renew", body={"ttl_s": 60}) if lid else None
    cl = api.req("POST", f"/v1/leases/{lid}/close") if lid else None
    rep.add("lease_create_renew_close", lid is not None and rn is not None
            and rn.status_code == 200 and cl is not None and cl.status_code == 200,
            status=r.status_code)  # fmt: skip

    saved: list[str] = []
    for i in range(2):
        r = api.req("POST", "/v1/tasks", headers=CLOUD_HEADERS,
                    body=chat(a.cloud_model, f"e2e restart {i}", max_tokens=a.long_tokens))  # fmt: skip
        if r.status_code == 202:
            saved.append(str(r.json()["id"]))
    unfinished = [t for t in saved if task_status(api, t) != "completed"]
    rep.add("restart_tasks_saved", len(saved) == 2, unfinished=len(unfinished))
    Path(a.state).write_text(json.dumps({"tasks": saved, "saved_at": time.time()}))


def _orphan_incidents(api: Api) -> list[str]:
    r = api.admin("GET", "/admin/status")
    if r.status_code != 200:
        return ["status_unavailable"]
    kinds = [str(i.get("kind")) for i in r.json().get("incidents", [])]
    return [k for k in kinds if "orphan" in k or "unknown" in k or "leak" in k]


def phase_post_restart(api: Api, a: argparse.Namespace, rep: Report) -> None:
    up = wait_until(lambda: _ok(api, "/healthz"), 120)
    rep.add("healthz_after_restart", up is not None)
    saved = json.loads(Path(a.state).read_text())["tasks"]
    present = [t for t in saved if task_status(api, t) is not None]
    rep.add("tasks_not_lost", len(present) == len(saved) == 2, found=len(present))
    done = wait_until(lambda: all(task_status(api, t) == "completed" for t in saved), a.wait_s)
    rep.add("tasks_completed", done is not None, seconds=None if done is None else round(done))
    if a.admin_key_file:
        bad = _orphan_incidents(api)
        rep.add("no_orphan_resources", not bad, incidents=bad)


def _ok(api: Api, path: str) -> bool:
    try:
        return api.req("GET", path, key=False, timeout=5).status_code == 200
    except httpx.HTTPError:
        return False


def _live_instances(api: Api) -> int | None:
    r = api.admin("GET", "/admin/status")
    if r.status_code != 200:
        return None
    pools = r.json().get("pools", {})
    return sum(1 for p in pools.values() if p.get("instance") is not None
               and p["instance"].get("state") not in ("STOPPED", None))  # fmt: skip


def _teardown_rows(api: Api) -> int | None:
    """STOPPING / PENDING_DELETE / LEAKED rows (a resource may still exist); None = unknown."""
    r = api.admin("GET", "/admin/status")
    if r.status_code != 200:
        return None
    rows = r.json().get("teardown")
    if not isinstance(rows, dict):
        return None
    return sum(int(n) for n in rows.values())


def _kill_incidents(api: Api) -> list[str] | None:
    r = api.admin("GET", "/admin/status")
    if r.status_code != 200:
        return None
    kinds = [str(i.get("kind")) for i in r.json().get("incidents", [])]
    return [k for k in kinds if k == "kill_incomplete" or "leak" in k]


def _prewarm(api: Api, a: argparse.Namespace, rep: Report) -> str | None:
    """A client prewarm lease launches a GPU (an admin hold only keeps a live one warm)."""
    r = api.req("POST", "/v1/leases", body={"model": a.cloud_model, "ttl_s": 600, "prewarm": True})
    rep.add("prewarm_lease", r.status_code == 201, status=r.status_code)
    return str(r.json()["id"]) if r.status_code == 201 else None


def phase_kill(api: Api, a: argparse.Namespace, rep: Report) -> None:
    lease = _prewarm(api, a, rep)
    up = wait_until(lambda: (_live_instances(api) or 0) > 0, 300)
    rep.add("instance_up", up is not None)
    t0 = time.monotonic()
    r = api.admin("POST", "/admin/kill", {"reason": "e2e"})
    report = r.json() if r.status_code == 200 else {}
    rep.add("kill_call", r.status_code == 200, status=r.status_code)
    # REV2-3: the kill report itself must confirm zero resources (destroy acknowledged)
    remaining = report.get("remaining")
    rep.add("kill_completed", report.get("completed") is True and remaining == [],
            completed=report.get("completed"), remaining=remaining)  # fmt: skip
    # live rows AND rows still being torn down (PENDING_DELETE / LEAKED are hidden from pools)
    unknown = False

    def zero() -> bool:
        nonlocal unknown
        rows = _teardown_rows(api)
        unknown = rows is None  # an API without the teardown view cannot prove zero
        return unknown or (rows == 0 and _live_instances(api) == 0)

    gone = wait_until(zero, 60, step=0.5)
    if unknown:
        gone = None
    elapsed = round(time.monotonic() - t0, 1)
    rep.add("zero_resources_le_60s", gone is not None and elapsed <= 60, seconds=elapsed,
            teardown_rows=_teardown_rows(api))  # fmt: skip
    bad = _kill_incidents(api)
    rep.add("no_kill_incidents", bad == [], incidents=bad)
    r = api.admin("POST", "/admin/resume", {})
    rep.add("resume", r.status_code == 200 and r.json().get("halted") is False,
            status=r.status_code)  # fmt: skip
    if lease is not None:  # kill closed it already; closing again is idempotent
        api.req("POST", f"/v1/leases/{lease}/close", body={"final": True})


def phase_prewarm(api: Api, a: argparse.Namespace, rep: Report) -> None:
    """S10w helper: launch a Mock instance (BOOTING ~60 s) for the weights probe `--op auto`;
    close it afterwards with the printed lease id: `close-lease --lease ID`."""
    lease = _prewarm(api, a, rep)
    up = wait_until(lambda: (_live_instances(api) or 0) > 0, 120)
    rep.add("instance_up", up is not None, lease=lease)


def phase_close_lease(api: Api, a: argparse.Namespace, rep: Report) -> None:
    r = api.req("POST", f"/v1/leases/{a.lease}/close", body={"final": True})
    rep.add("lease_closed", r.status_code == 200, status=r.status_code)


PHASES = {
    "pre": phase_pre, "post-restart": phase_post_restart, "kill": phase_kill,
    "prewarm": phase_prewarm, "close-lease": phase_close_lease,
}  # fmt: skip


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="e2e_mock.py")
    ap.add_argument("phase", choices=sorted(PHASES))
    ap.add_argument("--url", default="http://10.8.0.1:8095")
    ap.add_argument("--key-file", required=True, help="e2e client key (mock_only)")
    ap.add_argument("--admin-key-file", default=None)
    ap.add_argument("--state", default="e2e-state.json")
    ap.add_argument("--cloud-model", default="qwen-27b-cloud")
    ap.add_argument("--local-model", default="qwen-9b")
    ap.add_argument("--limit", type=int, default=4, help="clients.e2e.max_active_jobs")
    ap.add_argument("--long-tokens", type=int, default=4000)
    ap.add_argument("--wait-s", type=float, default=600)
    ap.add_argument("--lease", default=None, help="close-lease: lease id from prewarm")
    ap.add_argument("--timeout", type=float, default=600)
    a = ap.parse_args(argv)
    try:
        key = read_key(a.key_file)
        admin = read_key(a.admin_key_file) if a.admin_key_file else None
    except (OSError, ValueError) as exc:
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 2
    api = Api(a.url, key, admin, a.timeout)
    rep = Report(a.phase)
    try:
        PHASES[a.phase](api, a, rep)
    except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
        rep.add("phase_error", False, error=type(exc).__name__)
    print(json.dumps(rep.as_dict(), indent=2))
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
