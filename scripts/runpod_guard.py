#!/usr/bin/env python3
"""Independent RunPod guard for the T-372 live test (plan 3.6; critic F2b/F3, advocate F3, D3).

Runs on the WORKSTATION, not on the server; stdlib only, Python >= 3.10 (the unit runs
/usr/bin/python3, 3.10 on the workstation; R3-1); no `llmhub` import, its own raw-JSON parsing,
so a bug in the adapter cannot blind it. Keys come from files (0600/0400) and are sent only as
`Authorization: Bearer`; they never reach stdout, stderr or any file.

    runpod_guard.py snapshot --key-file RO --out before.json
    runpod_guard.py diff before.json after.json
    runpod_guard.py record --key-file RO --dir tests/fixtures/runpod/recorded
    runpod_guard.py watch --key-file RO --before before.json [--once] [--state-dir DIR]
                          [--auto-destroy --rw-key-file RW --state-dir DIR [--until TIME]]
                          [--extras-every N] [--interval S]
    runpod_guard.py destroy-leftovers --key-file RW --before before.json [--yes]

Auto-destroy deletes ONLY pods that are not in the "before" snapshot and whose name matches the
llmhub label `^llmhub-[0-9a-f]{12}-[0-9a-f]{8}$`, and only when a threshold fires: such a pod is
older than 75 min, `currentSpendPerHr` > 0.30, or more than one live llmhub pod exists. Then all
of them are deleted (a failed DELETE does not stop the others). Volumes, templates, registry
auths and endpoints are only reported. Every delete is a line in `<state-dir>/actions.log`.

Robustness (T-372 review REV-S1/S2/S5/S9, R2-1): the delete path needs only `id`, `name` and
`desiredStatus` of a pod; `costPerHr` (number or numeric string) and `lastStartedAt` are
optional, a bad value is a STOP (exit 4) reported AFTER the checks and deletes, never a reason
to skip them. The pod age is `now - min(first seen, lastStartedAt)`; the first-seen times live
in `<state-dir>/first_seen.json` (0600), so `--state-dir` is required with `--auto-destroy`; an
unreadable state file is reset (STOP, exit 4). The guard expires: after `--until` (ISO-8601 or
epoch s; default `before.taken_at + 24 h`) it only reports. Without a readable `before.json`
it refuses to run (exit 8): auto-destroy would be off, so that is a STOP too.

REST budget (live 2026-11-05: RunPod REST answered 429 "rate limit exceeded for the hour
window" after ~1 h of a guard pass and a probe every 60 s): a watch pass reads `GET /pods` and
the account (GraphQL) every time, the report-only kinds (volumes, templates, registry auths,
endpoints) only every `--extras-every` passes (default 10; the pass counter lives in
`<state-dir>/passes.json`; without `--state-dir` every pass reads them). The timer runs every
120 s, so a pass costs 1 REST call and every 10th pass 5 (about 39 REST calls an hour).

Exit codes: 0 ok; 2 usage/key file; 3 new llmhub pods in diff; 4 parse error or guard state
reset (unexpected answer: STOP the test); 5 a new volume/template/registry auth/endpoint
(STOP); 6 a threshold fired (STOP); 7 API error, also a failed DELETE (STOP); 8 no usable
before snapshot: the guard is not watching (STOP); 9 REST rate limit (HTTP 429): the guard is
(partly) blind until the hour window resets (STOP; stop every other REST poller too).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REST = "https://rest.runpod.io/v1"
GRAPHQL = "https://api.runpod.io/graphql"
LABEL = re.compile(r"^llmhub-[0-9a-f]{12}-[0-9a-f]{8}$")
MAX_AGE_S = 75 * 60
MAX_SPEND_H = 0.30
MAX_PODS = 1
TIMEOUT_S = 20
USER_AGENT = "llmhub-guard/0.3.2"
DEFAULT_TTL_S = 24 * 3600  # --until default: before.taken_at + 24 h (REV-S9)
FIRST_SEEN = "first_seen.json"
PASSES = "passes.json"
DEFAULT_EXTRAS_EVERY = 10
DEFAULT_INTERVAL_S = 120
RATE_LIMITED = 9
UTC = dt.timezone.utc  # noqa: UP017 -- Python 3.10 (unit: /usr/bin/python3) has no dt.UTC (R3-1)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """REV-S7: never follow a redirect (urllib would forward the Bearer key to the new host)."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any,
                         newurl: str) -> None:  # fmt: skip
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


# no proxy from the environment, no redirects (the adapter: follow_redirects/trust_env False)
OPENER: Any = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
NOW = time.time  # test seam
EXTRA_KINDS = {  # kind -> REST path (reported only, never deleted)
    "volumes": "/networkvolumes",
    "templates": "/templates?includePublicTemplates=false&includeRunpodTemplates=false",
    "registry_auths": "/containerregistryauth",
    "endpoints": "/endpoints",
}
ALLOW = {  # `record`: only these fields are written (critic F7)
    "pods": ("id", "name", "desiredStatus", "costPerHr", "machineId", "image", "imageName",
             "lastStartedAt", "adjustedCostPerHr", "gpuCount", "interruptible", "ports",
             "containerDiskInGb", "volumeInGb", "lastStatusChange"),
    "networkvolumes": ("id", "name", "size", "dataCenterId"),
    "templates": ("id", "name", "imageName", "isPublic", "isServerless"),
    "billing": ("amount", "time", "podId", "diskSpaceBilledGb"),
}


class GuardError(Exception):
    def __init__(self, code: int, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status  # HTTP status of an API error


def parse_error(what: str) -> GuardError:
    return GuardError(4, f"parse error: {what}")


# ------------------------------------------------------------------ keys and HTTP
def read_key(path: str) -> str:
    p = Path(path)
    try:
        st = p.lstat()
    except OSError:
        raise GuardError(2, f"key file {path}: missing") from None
    if not stat.S_ISREG(st.st_mode):
        raise GuardError(2, f"key file {path}: not a regular file")
    if st.st_mode & 0o077:
        raise GuardError(2, f"key file {path}: mode must be 0600 or 0400")
    key = p.read_text(encoding="utf-8").split("\n", 1)[0].strip()
    if not key:
        raise GuardError(2, f"key file {path}: empty")
    return key


def http_json(method: str, url: str, key: str, body: Any = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    # The REST edge refuses urllib's default "Python-urllib/3.x" User-Agent with 403
    # (seen in L0 on 2026-11-04; curl and httpx agents get 200), so always send our own.
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            after = exc.headers.get("Retry-After") if exc.headers is not None else None
            hint = f", Retry-After {after}" if after else ""
            raise GuardError(RATE_LIMITED, f"{method} {_path(url)} -> HTTP 429 (REST rate limit"
                             f"{hint})", 429) from None  # fmt: skip
        raise GuardError(7, f"{method} {_path(url)} -> HTTP {exc.code}", exc.code) from None
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise GuardError(7, f"{method} {_path(url)} -> {type(exc).__name__}") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise parse_error(f"{_path(url)}: not JSON") from None


def _path(url: str) -> str:
    return url.split("?", 1)[0].replace(REST, "").replace(GRAPHQL, "graphql")


# ------------------------------------------------------------------ raw parsing
def _str(d: dict[str, Any], k: str, where: str) -> str:
    v = d.get(k)
    if not isinstance(v, str):
        raise parse_error(f"{where}.{k}")
    return v


def _num(d: dict[str, Any], k: str, where: str) -> float:
    v = d.get(k)
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise parse_error(f"{where}.{k}")
    return float(v)


def _time(v: Any, where: str) -> float:
    if isinstance(v, bool):
        raise parse_error(where)
    if isinstance(v, int | float):
        return float(v) / 1000 if v > 1e11 else float(v)
    if not isinstance(v, str):
        raise parse_error(where)
    s = v.strip().removesuffix(" UTC").strip()
    s = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
    # Python 3.10 fromisoformat takes exactly 3 or 6 fraction digits: pad/cut to 6 (R3-1)
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    for parse in (dt.datetime.fromisoformat,
                  lambda x: dt.datetime.strptime(x, "%Y-%m-%d %H:%M:%S.%f %z"),
                  lambda x: dt.datetime.strptime(x, "%Y-%m-%d %H:%M:%S %z")):  # fmt: skip
        try:
            t = parse(s)
        except ValueError:
            continue
        return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()
    raise parse_error(where)


def _cost(v: Any) -> float | None:
    """`costPerHr`: openapi says number, its example is the string "0.74" (REV-S5)."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            v = float(v.strip())
        except ValueError:
            raise parse_error("pod.costPerHr") from None
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v):
        raise parse_error("pod.costPerHr")
    return float(v)


def _split_pods(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """-> (pods, their raw items, skipped notes). Only `id`, `name`, `desiredStatus` matter
    (REV-S1). A pod named with our label must have all three (strict: it may be ours to
    delete). Any other item (no string `name`, a foreign name) with a missing field is skipped
    with a note: openapi `Pod` has no `required`, and a malformed FOREIGN pod must not blind
    the guard to ours (R3-6)."""
    if raw is None:  # the real REST API answers JSON `null` for an empty list (L0 2026-11-04)
        raw = []
    if not isinstance(raw, list):
        raise parse_error("pods: not a list")
    pods: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    notes: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            notes.append("parse error: pods[]: not an object (skipped as foreign)")
            continue
        name = item.get("name")
        if isinstance(name, str) and LABEL.match(name):
            pods.append({"id": _str(item, "id", "pod"), "name": name,
                         "status": _str(item, "desiredStatus", "pod")})  # fmt: skip
            items.append(item)
            continue
        pid, status = item.get("id"), item.get("desiredStatus")
        if isinstance(name, str) and isinstance(pid, str) and isinstance(status, str):
            pods.append({"id": pid, "name": name, "status": status})
            items.append(item)
            continue
        notes.append(f"parse error: foreign pod {pid if isinstance(pid, str) else '?'}: "
                     "no string name/id/desiredStatus (skipped)")  # fmt: skip
    return pods, items, notes


def parse_pods_min(raw: Any) -> list[dict[str, Any]]:
    """Only what a deletion needs: `id`, `name`, `desiredStatus` (REV-S1). Extra fields of
    any shape never block it; malformed foreign items are skipped (R3-6, see `_split_pods`)."""
    pods, _, notes = _split_pods(raw)
    for n in notes:
        print(f"note: {n}", file=sys.stderr)
    return pods


def parse_pods_soft(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """`parse_pods_min` + optional `cost` and `started_at`; a bad optional value becomes None
    and an error line (the caller STOPs after its checks), never an exception."""
    pods, items, errors = _split_pods(raw)  # skipped foreign items: STOP after the checks
    for pod, item in zip(pods, items, strict=True):
        try:
            pod["cost"] = _cost(item.get("costPerHr"))
        except GuardError as exc:
            pod["cost"] = None
            errors.append(f"{exc} ({pod['id']})")
        started = item.get("lastStartedAt") or None  # absent while the image is pulled
        try:
            pod["started_at"] = None if started is None else _time(started, "pod.lastStartedAt")
        except GuardError as exc:
            pod["started_at"] = None
            errors.append(f"{exc} ({pod['id']})")
    return pods, errors


def parse_pods(raw: Any) -> list[dict[str, Any]]:
    """Strict form of `parse_pods_soft` (tests, `record`): any bad optional value raises."""
    pods, errors = parse_pods_soft(raw)
    if errors:
        raise parse_error(errors[0].removeprefix("parse error: "))
    return pods


def parse_named(raw: Any, kind: str) -> list[dict[str, Any]]:
    if raw is None:  # JSON `null` = empty list on the real API (containerregistryauth, L0)
        raw = []
    if not isinstance(raw, list):
        raise parse_error(f"{kind}: not a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise parse_error(f"{kind}[]")
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise parse_error(f"{kind}.name")
        out.append({"id": _str(item, "id", kind), "name": name or ""})
    return out


def fetch_account(key: str) -> tuple[float, float]:
    raw = http_json("POST", GRAPHQL, key,
                    {"query": "query { myself { clientBalance currentSpendPerHr } }"})  # fmt: skip
    if not isinstance(raw, dict) or raw.get("errors"):
        raise parse_error("myself") if not isinstance(raw, dict) else GuardError(7, "graphql error")
    me = (raw.get("data") or {}).get("myself") if isinstance(raw.get("data"), dict) else None
    if not isinstance(me, dict):
        raise parse_error("myself")
    return _num(me, "clientBalance", "myself"), _num(me, "currentSpendPerHr", "myself")


def take_snapshot(key: str, *, extras: bool = True) -> dict[str, Any]:
    """Pods first (a failure there is fatal: nothing can be decided); the account and the
    report-only kinds are soft: a failure is an entry of `errors` (STOP after the checks).
    `extras=False` skips the report-only kinds (absent from the snapshot: not compared).
    A 429 anywhere sets `rate_limited` (exit 9 after the checks)."""
    pods, errors = parse_pods_soft(http_json("GET", REST + "/pods", key))
    limited = False
    balance: float | None = None
    spend: float | None = None
    try:
        balance, spend = fetch_account(key)
    except GuardError as exc:
        errors.append(f"account: {exc}")
        limited = limited or exc.code == RATE_LIMITED
    snap: dict[str, Any] = {"taken_at": NOW(), "balance": balance, "spend_per_hr": spend,
                            "pods": pods}  # fmt: skip
    for kind, path in EXTRA_KINDS.items() if extras else ():
        try:
            snap[kind] = parse_named(http_json("GET", REST + path, key), kind)
        except GuardError as exc:
            snap[kind] = []
            errors.append(f"{kind}: {exc}")
            limited = limited or exc.code == RATE_LIMITED
    snap["errors"] = errors
    snap["rate_limited"] = limited
    return snap


# ------------------------------------------------------------------ logic
def _ids(snap: dict[str, Any], kind: str) -> set[str]:
    return {str(x["id"]) for x in snap.get(kind, [])}


def diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"new": {}, "gone": {}}
    for kind in ("pods", *EXTRA_KINDS):
        if kind not in b:  # not read in this pass (`--extras-every`): nothing to compare
            out["new"][kind], out["gone"][kind] = [], []
            continue
        ai, bi = _ids(a, kind), _ids(b, kind)
        out["new"][kind] = [x for x in b.get(kind, []) if x["id"] not in ai]
        out["gone"][kind] = [x for x in a.get(kind, []) if x["id"] not in bi]
    known = a.get("balance") is not None and b.get("balance") is not None
    out["balance_delta_cents"] = (round((float(b["balance"]) - float(a["balance"])) * 100)
                                  if known else None)  # fmt: skip
    out["spend_per_hr"] = None if b.get("spend_per_hr") is None else float(b["spend_per_hr"])
    return out


def diff_code(d: dict[str, Any]) -> int:
    if any(d["new"][k] for k in EXTRA_KINDS):
        return 5
    if any(str(p["name"]).startswith("llmhub-") for p in d["new"]["pods"]):
        return 3
    return 0


def candidates(before: dict[str, Any], now_snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Pods we may delete: new since `before`, llmhub label, not TERMINATED."""
    old = _ids(before, "pods")
    return [p for p in now_snap["pods"] if p["id"] not in old and LABEL.match(p["name"])
            and p["status"].upper() != "TERMINATED"]  # fmt: skip


def pod_age(p: dict[str, Any], now: float, first_seen: dict[str, float] | None) -> float | None:
    """`now - min(first seen, lastStartedAt)` (REV-S2): a pod that still pulls its image has
    no `lastStartedAt` (it bills already), a restart resets it; the first sight never moves."""
    times = [t for t in ((first_seen or {}).get(p["id"]), p.get("started_at")) if t is not None]
    return now - min(times) if times else None


def triggers(before: dict[str, Any], now_snap: dict[str, Any], now: float,
             first_seen: dict[str, float] | None = None) -> list[str]:  # fmt: skip
    live = [p for p in now_snap["pods"] if LABEL.match(p["name"])
            and p["status"].upper() != "TERMINATED"]  # fmt: skip
    out = []
    ages = [pod_age(p, now, first_seen) for p in candidates(before, now_snap)]
    if any(a is not None and a > MAX_AGE_S for a in ages):
        out.append("age>75min")
    spend = now_snap.get("spend_per_hr")
    if spend is not None and float(spend) > MAX_SPEND_H:
        out.append(f"spend>{MAX_SPEND_H}")
    if len(live) > MAX_PODS:
        out.append(f"pods>{MAX_PODS}")
    return out


def _log_action(state_dir: Path | None, text: str) -> None:
    if state_dir is None:
        return
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.fromtimestamp(NOW(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(state_dir / "actions.log", "a", encoding="utf-8") as f:
        f.write(f"{stamp} {text}\n")


def delete_pod(pod_id: str, rw_key: str) -> str:
    """"deleted" or "gone". RunPod documents DELETE /pods/{id} -> 204/400/401 (no 404): a 400
    or 404 is checked with GET /pods/{id}, whose 404 alone means gone (REV-S4/R2-7). A DELETE
    404 is never taken as success by itself (R3-2): a wrong path on a live pod would lie."""
    try:
        http_json("DELETE", f"{REST}/pods/{pod_id}", rw_key)
        return "deleted"
    except GuardError as exc:
        if exc.status not in (400, 404):
            raise
        try:
            http_json("GET", f"{REST}/pods/{pod_id}", rw_key)
        except GuardError as get_exc:
            if get_exc.status == 404:
                return "gone"
        raise


def destroy(pods: list[dict[str, Any]], rw_key: str, state_dir: Path | None, why: str
            ) -> tuple[int, list[str]]:  # fmt: skip
    """Delete every pod (REV-S8: one failure never stops the others) -> (done, failures)."""
    n = 0
    failed: list[str] = []
    for p in pods:
        if not LABEL.match(p["name"]):  # belt and braces: never a foreign pod
            continue
        try:
            result = delete_pod(p["id"], rw_key)
        except GuardError as exc:
            failed.append(p["id"])
            _log_action(state_dir, f"destroy pod {p['id']} {p['name']} FAILED: {exc} ({why})")
            print(f"STOP: destroy pod {p['id']} {p['name']} FAILED: {exc} ({why})")
            continue
        _log_action(state_dir, f"destroy pod {p['id']} {p['name']} {result} ({why})")
        print(f"destroyed pod {p['id']} {p['name']} {result} ({why})")
        n += 1
    return n, failed


def load_snapshot(path: str) -> dict[str, Any]:
    try:
        snap = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GuardError(2, f"cannot read snapshot {path}") from None
    if not isinstance(snap, dict) or "pods" not in snap or "balance" not in snap:
        raise GuardError(2, f"{path}: not a guard snapshot")
    return snap


def load_first_seen(state_dir: Path) -> tuple[dict[str, float], bool]:
    """-> (first-seen times, reset). A missing file is the first pass; an unreadable or
    malformed one is reset to empty (every pod is seen "now") and reported as a STOP."""
    path = state_dir / FIRST_SEEN
    if not path.exists():
        return {}, False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, True
    if not isinstance(doc, dict) or not all(
            isinstance(k, str) and isinstance(v, int | float) and not isinstance(v, bool)
            for k, v in doc.items()):  # fmt: skip
        return {}, True
    return {k: float(v) for k, v in doc.items()}, False


def update_first_seen(seen: dict[str, float], pods: list[dict[str, Any]], now: float
                      ) -> dict[str, float]:  # fmt: skip
    """Keep the first sight of every pod listed now; forget pods that are gone."""
    return {p["id"]: seen.get(p["id"], now) for p in pods}


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    tmp.replace(path)


# ------------------------------------------------------------------ commands
def _fmt(v: float | None, spec: str) -> str:
    return "?" if v is None else format(v, spec)


def _report_errors(errors: list[str]) -> None:
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
        print(f"STOP: {e}")


def cmd_snapshot(a: argparse.Namespace) -> int:
    snap = take_snapshot(read_key(a.key_file))
    _write_json(Path(a.out), snap)
    print(f"snapshot: pods={len(snap['pods'])} volumes={len(snap['volumes'])} "
          f"templates={len(snap['templates'])} balance={_fmt(snap['balance'], '.2f')} "
          f"spend/h={_fmt(snap['spend_per_hr'], '.3f')}")  # fmt: skip
    _report_errors(snap["errors"])
    return 4 if snap["errors"] else 0


def _print_diff(d: dict[str, Any]) -> None:
    for side in ("new", "gone"):
        for kind, items in d[side].items():
            for x in items:
                print(f"{side} {kind} {x['id']} {x.get('name', '')}")
    print(f"balance_delta_cents={d['balance_delta_cents']} "
          f"spend_per_hr={_fmt(d['spend_per_hr'], '.3f')}")


def cmd_diff(a: argparse.Namespace) -> int:
    d = diff(load_snapshot(a.a), load_snapshot(a.b))
    _print_diff(d)
    return diff_code(d)


def _allow(item: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise parse_error("record item")
    return {k: item[k] for k in fields if k in item}


def cmd_record(a: argparse.Namespace) -> int:
    key = read_key(a.key_file)
    out = Path(a.dir)
    now = NOW()
    start = dt.datetime.fromtimestamp(now - 86400, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = dt.datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sources = {
        "pods": "/pods", "networkvolumes": "/networkvolumes",
        "templates": EXTRA_KINDS["templates"],
        "billing": f"/billing/pods?startTime={start}&endTime={end}&bucketSize=hour&grouping=podId",
    }  # fmt: skip
    for kind, path in sources.items():
        raw = http_json("GET", REST + path, key)
        if raw is None:  # JSON `null` = empty list
            raw = []
        if not isinstance(raw, list):
            raise parse_error(f"{kind}: not a list")
        doc = {"_source": "recorded", "_kind": kind,
               "items": [_allow(x, ALLOW[kind]) for x in raw]}  # fmt: skip
        _write_json(out / f"{kind}.json", doc)
        print(f"recorded {kind}: {len(raw)} item(s)")
    return 0


def parse_until(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return _time(text, "--until")
    except GuardError:
        raise GuardError(2, f"--until {text!r}: not ISO-8601 or epoch seconds") from None


def _no_before(path: str, why: str) -> GuardError:
    msg = (f"STOP: no usable before snapshot {path} ({why}): the guard is NOT watching, "
           "auto-destroy is off")  # fmt: skip
    print(msg, flush=True)
    return GuardError(8, msg)


def next_pass(state: Path | None, every: int) -> bool:
    """Count this pass in `<state>/passes.json`; True when the report-only kinds are due
    (pass 0, every-th pass, no state dir, or an unreadable counter)."""
    if state is None or every <= 1:
        return True
    path = state / PASSES
    n = 0
    if path.exists():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            n = int(doc["n"]) if isinstance(doc, dict) else 0
        except (OSError, ValueError, KeyError, TypeError):
            n = 0
    _write_json(path, {"n": n + 1})
    return n % every == 0


def watch_pass(key: str, rw: str | None, before: dict[str, Any], state: Path | None,
               until: float, extras_every: int = DEFAULT_EXTRAS_EVERY) -> int:
    """One pass: snapshot, report, thresholds, deletes; STOP codes only after the deletes."""
    extras = next_pass(state, extras_every)
    snap = take_snapshot(key, extras=extras)
    now = NOW()
    d = diff(before, snap)
    seen: dict[str, float] = {}
    reset = False
    if state is not None:
        seen, reset = load_first_seen(state)
        seen = update_first_seen(seen, snap["pods"], now)
        _write_json(state / FIRST_SEEN, seen)
    ours = [p for p in snap["pods"] if LABEL.match(p["name"])]
    stamp = dt.datetime.fromtimestamp(now, UTC).strftime("%H:%M:%SZ")
    extra = sum(len(d["new"][k]) for k in EXTRA_KINDS)
    print(f"{stamp} llmhub_pods={len(ours)} spend/h={_fmt(snap['spend_per_hr'], '.3f')} "
          f"balance_delta_cents={d['balance_delta_cents']} "
          f"new_other={extra if extras else 'skipped'}", flush=True)  # fmt: skip
    for k in EXTRA_KINDS:
        for x in d["new"][k]:
            print(f"STOP: new {k} {x['id']} {x['name']} (reported, not deleted)")
    fired = triggers(before, snap, now, seen if state is not None else None)
    failed: list[str] = []
    if fired:
        print(f"STOP: threshold {','.join(fired)}")
        todo = candidates(before, snap)
        if rw is not None and todo and now >= until:
            print(f"guard expired at {until:.0f}: report only, {len(todo)} pod(s) NOT deleted")
        elif rw is not None and todo:
            _, failed = destroy(todo, rw, state, ",".join(fired))
    if reset:
        print("STOP: guard state reset (first_seen.json unreadable; ages restart now)")
    _report_errors(snap["errors"])
    if snap.get("rate_limited"):
        print("STOP: RunPod REST rate limit (HTTP 429): stop other REST pollers and wait for "
              "the hour window")  # fmt: skip
    if failed:
        return 7
    if fired:
        return 6
    if extra:
        return 5
    if snap.get("rate_limited"):
        return RATE_LIMITED
    return 4 if reset or snap["errors"] else 0


def cmd_watch(a: argparse.Namespace) -> int:
    key = read_key(a.key_file)
    rw = read_key(a.rw_key_file) if a.auto_destroy else None
    if a.auto_destroy and rw is None:
        raise GuardError(2, "--auto-destroy needs --rw-key-file")
    try:
        before = load_snapshot(a.before)
    except GuardError as exc:
        raise _no_before(a.before, str(exc)) from None
    state = Path(a.state_dir) if a.state_dir else None
    if a.until is not None:
        until = parse_until(a.until)
    else:
        taken = before.get("taken_at")
        if not isinstance(taken, int | float) or isinstance(taken, bool):
            raise _no_before(a.before, "no taken_at")
        until = float(taken) + DEFAULT_TTL_S
    if rw is not None and NOW() >= until:
        print(f"guard expired at {until:.0f}: report only")
    while True:
        code = watch_pass(key, rw, before, state, until, a.extras_every)
        if a.once or code:
            return code
        time.sleep(a.interval)


def cmd_destroy_leftovers(a: argparse.Namespace) -> int:
    """Emergency path: only `id`/`name`/`desiredStatus` of `GET /pods` are needed (REV-S1);
    the account, the other kinds and the optional pod fields are not even read."""
    rw = read_key(a.key_file)
    before = load_snapshot(a.before)
    todo = candidates(before, {"pods": parse_pods_min(http_json("GET", REST + "/pods", rw))})
    if not a.yes:
        for p in todo:
            print(f"would destroy pod {p['id']} {p['name']}")
        print(f"dry run: {len(todo)} pod(s); add --yes")
        return 0
    n, failed = destroy(todo, rw, Path(a.state_dir) if a.state_dir else None,
                        "destroy-leftovers")  # fmt: skip
    print(f"destroyed {n} pod(s)" + (f"; FAILED {len(failed)}: {' '.join(failed)}" if failed
                                     else ""))  # fmt: skip
    return 7 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="runpod_guard.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot")
    s.add_argument("--key-file", required=True)
    s.add_argument("--out", required=True)
    d = sub.add_parser("diff")
    d.add_argument("a")
    d.add_argument("b")
    r = sub.add_parser("record")
    r.add_argument("--key-file", required=True)
    r.add_argument("--dir", required=True)
    w = sub.add_parser("watch")
    w.add_argument("--key-file", required=True)
    w.add_argument("--before", required=True)
    w.add_argument("--once", action="store_true")
    w.add_argument("--auto-destroy", action="store_true")
    w.add_argument("--rw-key-file", default=None)
    w.add_argument("--state-dir", default=None)
    w.add_argument("--until", default=None,
                   help="after this time (ISO-8601 or epoch s) only report, never delete")
    w.add_argument("--extras-every", type=int, default=DEFAULT_EXTRAS_EVERY,
                   help="read volumes/templates/registry auths/endpoints every N-th pass")
    w.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_S,
                   help="seconds between passes without --once (REST budget)")
    x = sub.add_parser("destroy-leftovers")
    x.add_argument("--key-file", required=True)
    x.add_argument("--before", required=True)
    x.add_argument("--yes", action="store_true")
    x.add_argument("--state-dir", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "watch" and a.auto_destroy and not a.rw_key_file:
        print("error: --auto-destroy needs --rw-key-file", file=sys.stderr)
        return 2
    if a.cmd == "watch" and (a.extras_every < 1 or a.interval < 30):
        print("error: --extras-every must be >= 1 and --interval >= 30", file=sys.stderr)
        return 2
    if a.cmd == "watch" and a.auto_destroy and not a.state_dir:
        print("error: --auto-destroy needs --state-dir (pod ages persist there)", file=sys.stderr)
        return 2
    fn = {"snapshot": cmd_snapshot, "diff": cmd_diff, "record": cmd_record, "watch": cmd_watch,
          "destroy-leftovers": cmd_destroy_leftovers}[a.cmd]  # fmt: skip
    try:
        return fn(a)
    except GuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
