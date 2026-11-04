"""llmhub tasks and leases over raw HTTP (httpx): what a non-OpenAI client does.

    LLMHUB_URL=http://10.8.0.1:8095 LLMHUB_KEY=lh_... python raw_tasks.py

- POST /v1/tasks -> 202 + id; poll GET /v1/tasks/{id} (sleep eta_s, bounded), then /result;
- a retry after a network error re-sends the same Idempotency-Key: no duplicate task;
- leases: renew more often than ttl_s, close with final=true when done.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

MODEL = os.environ.get("LLMHUB_MODEL", "auto")
LEASE_MODEL = os.environ.get("LLMHUB_LEASE_MODEL", "big")


def submit(http: httpx.Client, body: dict[str, object], idem: str) -> dict[str, object]:
    for attempt in range(3):
        try:
            r = http.post("/v1/tasks", json=body, headers={"Idempotency-Key": idem})
        except httpx.TransportError:
            time.sleep(2**attempt)  # same key: the server returns the same task
            continue
        if r.status_code in (200, 202):
            return r.json()
        error = r.json()["error"]
        if not error["retryable"]:
            raise SystemExit(f"submit failed: {error['code']} ({error.get('reason')})")
        time.sleep(error.get("retry_after_s") or 5)
    raise SystemExit("submit failed after retries")


def wait_result(http: httpx.Client, task_id: str, limit_s: float = 3600) -> dict[str, object]:
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        st = http.get(f"/v1/tasks/{task_id}").json()
        if st["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(min(30.0, max(0.5, float(st.get("eta_s") or 1) / 4)))
    r = http.get(f"/v1/tasks/{task_id}/result")
    if r.status_code != 200:
        raise SystemExit(f"task {task_id}: {r.json()['error']['code']}")
    return r.json()


def main() -> int:
    base, key = os.environ["LLMHUB_URL"], os.environ["LLMHUB_KEY"]
    with httpx.Client(
        base_url=base, headers={"Authorization": f"Bearer {key}"}, timeout=60
    ) as http:
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Summarize llmhub in one line"}],
            "llmhub": {"priority": "normal", "policy": "ECONOMY", "deadline_s": 7200},
        }
        task = submit(http, body, idem=f"example-{uuid.uuid4()}")
        print("task:", task["id"], task["status"], task.get("route"))
        result = wait_result(http, str(task["id"]))
        print("answer:", result["choices"][0]["message"]["content"])  # type: ignore[index]

        lease = http.post("/v1/leases", json={"model": LEASE_MODEL, "ttl_s": 120})
        if lease.status_code == 201:
            lid = lease.json()["id"]
            http.post(f"/v1/leases/{lid}/renew").raise_for_status()  # heartbeat
            http.post(f"/v1/leases/{lid}/close", json={"final": True}).raise_for_status()
            print("lease:", lid, "renewed and closed")
        else:
            print("no lease:", lease.json()["error"]["code"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
