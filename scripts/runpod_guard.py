#!/usr/bin/env python3
"""Independent RunPod guard for the T-372 live test (plan 3.6; critic F2b/F3, advocate F3, D3).

Runs on the WORKSTATION, not on the server; stdlib only (no `llmhub` import, its own raw-JSON
parsing), so a bug in the adapter cannot blind it. Keys come from files (0600/0400) and are
sent only as `Authorization: Bearer`; they never reach stdout, stderr or any file.

    runpod_guard.py snapshot --key-file RO --out before.json
    runpod_guard.py diff before.json after.json
    runpod_guard.py record --key-file RO --dir tests/fixtures/runpod/recorded
    runpod_guard.py watch --key-file RO --before before.json [--once]
                          [--auto-destroy --rw-key-file RW] [--state-dir DIR]
    runpod_guard.py destroy-leftovers --key-file RW --before before.json [--yes]

Auto-destroy deletes ONLY pods that are not in the "before" snapshot and whose name matches the
llmhub label `^llmhub-[0-9a-f]{12}-[0-9a-f]{8}$`, and only when a threshold fires: such a pod is
older than 75 min, `currentSpendPerHr` > 0.30, or more than one live llmhub pod exists. Then all
of them are deleted. Volumes, templates, registry auths and endpoints are only reported.
Every delete is a line in `<state-dir>/actions.log` (no keys).

Exit codes: 0 ok; 2 usage/key file; 3 new llmhub pods in diff; 4 parse error (unexpected
answer: STOP the test); 5 a new volume/template/registry auth/endpoint (STOP); 6 auto-destroy
fired (STOP); 7 API error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
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
OPENER: Any = urllib.request.build_opener()  # test seam
NOW = time.time  # test seam
EXTRA_KINDS = {  # kind -> REST path (reported only, never deleted)
    "volumes": "/networkvolumes",
    "templates": "/templates?includePublicTemplates=false&includeRunpodTemplates=false",
    "registry_auths": "/containerregistryauth",
    "endpoints": "/endpoints",
}
ALLOW = {  # `record`: only these fields are written (critic F7)
    "pods": ("id", "name", "desiredStatus", "costPerHr", "machineId", "image", "imageName",
             "createdAt", "gpuCount", "interruptible", "ports", "containerDiskInGb",
             "volumeInGb", "lastStatusChange"),
    "networkvolumes": ("id", "name", "size", "dataCenterId"),
    "templates": ("id", "name", "imageName", "isPublic", "isServerless"),
    "billing": ("amount", "time", "podId", "diskSpaceBilledGb"),
}


class GuardError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


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
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with OPENER.open(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if method == "DELETE" and exc.code == 404:
            return None
        raise GuardError(7, f"{method} {_path(url)} -> HTTP {exc.code}") from None
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
    s = s[:-1] + "+00:00" if s.endswith("Z") else s
    for parse in (dt.datetime.fromisoformat,
                  lambda x: dt.datetime.strptime(x, "%Y-%m-%d %H:%M:%S.%f %z"),
                  lambda x: dt.datetime.strptime(x, "%Y-%m-%d %H:%M:%S %z")):  # fmt: skip
        try:
            t = parse(s)
        except ValueError:
            continue
        return (t if t.tzinfo else t.replace(tzinfo=dt.UTC)).timestamp()
    raise parse_error(where)


def parse_pods(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise parse_error("pods: not a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise parse_error("pods[]")
        out.append({
            "id": _str(item, "id", "pod"), "name": _str(item, "name", "pod"),
            "status": _str(item, "desiredStatus", "pod"), "cost": _num(item, "costPerHr", "pod"),
            "created_at": _time(item.get("createdAt"), "pod.createdAt"),
        })  # fmt: skip
    return out


def parse_named(raw: Any, kind: str) -> list[dict[str, Any]]:
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


def take_snapshot(key: str) -> dict[str, Any]:
    balance, spend = fetch_account(key)
    snap: dict[str, Any] = {
        "taken_at": NOW(), "balance": balance, "spend_per_hr": spend,
        "pods": parse_pods(http_json("GET", REST + "/pods", key)),
    }  # fmt: skip
    for kind, path in EXTRA_KINDS.items():
        snap[kind] = parse_named(http_json("GET", REST + path, key), kind)
    return snap


# ------------------------------------------------------------------ logic
def _ids(snap: dict[str, Any], kind: str) -> set[str]:
    return {str(x["id"]) for x in snap.get(kind, [])}


def diff(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"new": {}, "gone": {}}
    for kind in ("pods", *EXTRA_KINDS):
        ai, bi = _ids(a, kind), _ids(b, kind)
        out["new"][kind] = [x for x in b.get(kind, []) if x["id"] not in ai]
        out["gone"][kind] = [x for x in a.get(kind, []) if x["id"] not in bi]
    out["balance_delta_cents"] = round((float(b["balance"]) - float(a["balance"])) * 100)
    out["spend_per_hr"] = float(b["spend_per_hr"])
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


def triggers(before: dict[str, Any], now_snap: dict[str, Any], now: float) -> list[str]:
    live = [p for p in now_snap["pods"] if LABEL.match(p["name"])
            and p["status"].upper() != "TERMINATED"]  # fmt: skip
    out = []
    if any(now - p["created_at"] > MAX_AGE_S for p in candidates(before, now_snap)):
        out.append("age>75min")
    if float(now_snap["spend_per_hr"]) > MAX_SPEND_H:
        out.append(f"spend>{MAX_SPEND_H}")
    if len(live) > MAX_PODS:
        out.append(f"pods>{MAX_PODS}")
    return out


def _log_action(state_dir: Path | None, text: str) -> None:
    if state_dir is None:
        return
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.fromtimestamp(NOW(), dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(state_dir / "actions.log", "a", encoding="utf-8") as f:
        f.write(f"{stamp} {text}\n")


def destroy(pods: list[dict[str, Any]], rw_key: str, state_dir: Path | None, why: str) -> int:
    n = 0
    for p in pods:
        if not LABEL.match(p["name"]):  # belt and braces: never a foreign pod
            continue
        http_json("DELETE", f"{REST}/pods/{p['id']}", rw_key)
        _log_action(state_dir, f"destroy pod {p['id']} {p['name']} ({why})")
        print(f"destroyed pod {p['id']} {p['name']} ({why})")
        n += 1
    return n


def load_snapshot(path: str) -> dict[str, Any]:
    try:
        snap = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise GuardError(2, f"cannot read snapshot {path}") from None
    if not isinstance(snap, dict) or "pods" not in snap or "balance" not in snap:
        raise GuardError(2, f"{path}: not a guard snapshot")
    return snap


def _write_json(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=1, sort_keys=True)
    tmp.replace(path)


# ------------------------------------------------------------------ commands
def cmd_snapshot(a: argparse.Namespace) -> int:
    snap = take_snapshot(read_key(a.key_file))
    _write_json(Path(a.out), snap)
    print(f"snapshot: pods={len(snap['pods'])} volumes={len(snap['volumes'])} "
          f"templates={len(snap['templates'])} balance={snap['balance']:.2f} "
          f"spend/h={snap['spend_per_hr']:.3f}")  # fmt: skip
    return 0


def _print_diff(d: dict[str, Any]) -> None:
    for side in ("new", "gone"):
        for kind, items in d[side].items():
            for x in items:
                print(f"{side} {kind} {x['id']} {x.get('name', '')}")
    print(f"balance_delta_cents={d['balance_delta_cents']} spend_per_hr={d['spend_per_hr']:.3f}")


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
    start = dt.datetime.fromtimestamp(now - 86400, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = dt.datetime.fromtimestamp(now, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sources = {
        "pods": "/pods", "networkvolumes": "/networkvolumes",
        "templates": EXTRA_KINDS["templates"],
        "billing": f"/billing/pods?startTime={start}&endTime={end}&bucketSize=hour&grouping=podId",
    }  # fmt: skip
    for kind, path in sources.items():
        raw = http_json("GET", REST + path, key)
        if not isinstance(raw, list):
            raise parse_error(f"{kind}: not a list")
        doc = {"_source": "recorded", "_kind": kind,
               "items": [_allow(x, ALLOW[kind]) for x in raw]}  # fmt: skip
        _write_json(out / f"{kind}.json", doc)
        print(f"recorded {kind}: {len(raw)} item(s)")
    return 0


def cmd_watch(a: argparse.Namespace) -> int:
    key = read_key(a.key_file)
    rw = read_key(a.rw_key_file) if a.auto_destroy else None
    if a.auto_destroy and rw is None:
        raise GuardError(2, "--auto-destroy needs --rw-key-file")
    before = load_snapshot(a.before)
    state = Path(a.state_dir) if a.state_dir else None
    while True:
        snap = take_snapshot(key)
        d = diff(before, snap)
        now = NOW()
        ours = [p for p in snap["pods"] if LABEL.match(p["name"])]
        stamp = dt.datetime.fromtimestamp(now, dt.UTC).strftime("%H:%M:%SZ")
        extra = sum(len(d["new"][k]) for k in EXTRA_KINDS)
        print(f"{stamp} llmhub_pods={len(ours)} spend/h={snap['spend_per_hr']:.3f} "
              f"balance_delta_cents={d['balance_delta_cents']} new_other={extra}", flush=True)  # fmt: skip
        code = 0
        if extra:
            for k in EXTRA_KINDS:
                for x in d["new"][k]:
                    print(f"STOP: new {k} {x['id']} {x['name']} (reported, not deleted)")
            code = 5
        fired = triggers(before, snap, now)
        if fired:
            print(f"STOP: threshold {','.join(fired)}")
            if rw is not None:
                todo = candidates(before, snap)
                if todo:
                    destroy(todo, rw, state, ",".join(fired))
                    code = 6
            code = code or 6
        if a.once or code:
            return code
        time.sleep(60)


def cmd_destroy_leftovers(a: argparse.Namespace) -> int:
    rw = read_key(a.key_file)
    before = load_snapshot(a.before)
    todo = candidates(before, take_snapshot(rw))
    if not a.yes:
        for p in todo:
            print(f"would destroy pod {p['id']} {p['name']}")
        print(f"dry run: {len(todo)} pod(s); add --yes")
        return 0
    n = destroy(todo, rw, Path(a.state_dir) if a.state_dir else None, "destroy-leftovers")
    print(f"destroyed {n} pod(s)")
    return 0


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
    x = sub.add_parser("destroy-leftovers")
    x.add_argument("--key-file", required=True)
    x.add_argument("--before", required=True)
    x.add_argument("--yes", action="store_true")
    x.add_argument("--state-dir", default=None)
    a = ap.parse_args(argv)
    if a.cmd == "watch" and a.auto_destroy and not a.rw_key_file:
        print("error: --auto-destroy needs --rw-key-file", file=sys.stderr)
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
