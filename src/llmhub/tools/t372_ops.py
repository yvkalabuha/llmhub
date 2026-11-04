"""Operator helpers of the RunPod live test (T-372 D2/D3). Used by `admin_cli`.

- `ledger_exclude`: mark the ledger rows of a provider excluded (backup first, one
  transaction, nothing deleted; critic F14 / customer Q5);
- `provider_check`: read-only view of a provider (offers, owned resources, balance, billing);
- `lifecycle_probe`: one small pod through `RunpodAdapter` (L2b, advocate F10): a row of the
  reserved pool `_probe` (so the reaper never sees an unknown resource), create -> visible ->
  desiredStatus RUNNING -> held `hold_s` (R2-6: the real desiredStatus is RUNNING right after
  POST; the hold lets `guard watch --once` and `guard record` see a real pod) -> destroy
  (always, in `finally`, the known ref first, R2-2) -> zero; prints timings only;
- `scan_secrets`: count secret-looking strings of one op (pod logs on stdin);
- `weights_token_check`: the HTTP status the weights server gives the op's token now and the
  verdict the DB implies; the proof of `revoked` is the weights-auth log line (R2-8).
Nothing here prints a key, a token or a pod env.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from llmhub.clock import Clock
from llmhub.configuration.models import PROBE_POOL, Config
from llmhub.persistence.db import tx
from llmhub.provider_adapters.base import (
    CreatedResourceRejected,
    InstanceSpec,
    OfferQuery,
    ProviderAdapter,
    ProviderError,
    RemoteStatus,
)
from llmhub.provider_adapters.labels import make_label, new_op_id

PROBE_CMD = ("sleep", "600")
PROBE_HOLD_S = 150.0  # >= 2 guard timer ticks (60 s) + `guard record` (R2-6)


class OpsError(Exception):
    """A refused operator action (message without secrets)."""


# ------------------------------------------------------------------ D2 ledger-exclude
@dataclass(frozen=True)
class ExcludeResult:
    provider: str
    rows: int
    usd: float
    dry_run: bool
    backup: str | None


def ledger_exclude(
    conn: sqlite3.Connection,
    cfg: Config,
    provider: str,
    *,
    reason: str,
    yes: bool,
    now: float,
    backup_dir: Path | None,
) -> ExcludeResult:
    if provider in cfg.providers.enabled:
        raise OpsError(f"{provider} is in providers.enabled; disable it in the config first")
    live = conn.execute(
        "SELECT count(*) FROM instances WHERE provider=? AND state != 'STOPPED'", (provider,)
    ).fetchone()[0]
    if live:
        raise OpsError(f"{live} {provider} instance(s) are not STOPPED")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(ledger)")}
    if "excluded_at" not in cols:
        raise OpsError("ledger has no excluded_at column (start the 0.3.0 release first)")
    row = conn.execute(
        "SELECT count(*) AS n, COALESCE(sum(amount_usd), 0) AS usd FROM ledger"
        " WHERE provider=? AND excluded_at IS NULL",
        (provider,),
    ).fetchone()
    n, usd = int(row["n"]), float(row["usd"])
    if not yes:
        return ExcludeResult(provider, n, round(usd, 6), True, None)
    backup: str | None = None
    if backup_dir is not None:
        from llmhub.persistence.backup import backup_state_db

        db = conn.execute("PRAGMA database_list").fetchone()["file"]
        backup = str(backup_state_db(Path(db), backup_dir, 14, datetime.fromtimestamp(now, UTC)))
    from llmhub.cost_tracker.txutil import add_incident

    with tx(conn):
        cur = conn.execute(
            "UPDATE ledger SET excluded_at=?, excluded_reason=? WHERE provider=?"
            " AND excluded_at IS NULL",
            (now, reason[:40], provider),
        )
        add_incident(
            conn, now, "ledger_excluded", "info", ref=provider,
            dedup_key=f"ledger_excluded:{provider}:{int(now)}",
            details_json=json.dumps({"rows": cur.rowcount, "usd": round(usd, 6),
                                     "reason": reason[:40], "backup": backup}),
        )  # fmt: skip
    return ExcludeResult(provider, n, round(usd, 6), False, backup)


# ------------------------------------------------------------------ D3 provider-check
async def provider_check(adapter: ProviderAdapter, cfg: Config, now: float) -> dict[str, Any]:
    """Read-only: offers of the provider's pools, owned resources, balance, billing."""
    out: dict[str, Any] = {"provider": adapter.name}
    offers: list[dict[str, Any]] = []
    for pool in cfg.pools.values():
        if pool.provider != adapter.name:
            continue
        q = OfferQuery(gpu=pool.gpu, min_vram_gb=pool.vram_gb,
                       max_price_usd_h=pool.max_price_usd_h, exclude_hosts=frozenset())  # fmt: skip
        for o in await adapter.list_offers(q):
            offers.append({"pool": pool.name, "gpu": o.gpu, "offer_id": o.offer_id,
                           "price_usd_h": o.price_usd_h, "disk_usd_h": round(o.disk_usd_h, 5),
                           "vram_gb": o.vram_gb})  # fmt: skip
    out["offers"] = offers
    owned = await adapter.list_owned("llmhub-")
    out["owned"] = [{"ref": r.ref, "label": r.label, "kind": r.kind.value,
                     "status": r.status.value} for r in owned]  # fmt: skip
    bal = await adapter.balance()
    out["balance_usd"] = None if bal is None else round(bal.usd, 4)
    month_start = datetime.fromtimestamp(now, UTC).replace(day=1, hour=0, minute=0, second=0,
                                                           microsecond=0).timestamp()  # fmt: skip
    recs = await adapter.billing_since(month_start)
    out["billing_month_usd"] = round(sum(r.amount_usd for r in recs), 4)
    out["billing_records"] = len(recs)
    out["billing_refs"] = sorted({r.ref for r in recs if r.ref})
    return out


# ------------------------------------------------------------------ D3 lifecycle-probe
@dataclass
class ProbeReport:
    label: str
    instance_id: str
    ref: str | None = None
    name_ok: bool | None = None
    cost_per_hr: float | None = None
    create_s: float | None = None
    visible_s: float | None = None
    desired_running_s: float | None = None  # desiredStatus RUNNING (desired, not actual)
    held_s: float | None = None  # create answer -> teardown start
    destroy_gone_s: float | None = None
    destroyed: bool = False
    zero: bool = False
    error: str | None = None
    notes: list[str] = field(default_factory=list[str])


def _insert_probe_row(conn: sqlite3.Connection, provider: str, op_id: str, label: str,
                      now: float, max_s: float) -> str:  # fmt: skip
    iid = f"inst-{op_id}"
    with tx(conn):
        conn.execute(
            "INSERT INTO instances(id, op_id, pool, provider, label, state, state_since,"
            " created_at, max_lifetime_at, accrued_to, create_called_at, attempts,"
            " first_create_called_at, last_create_called_at)"
            " VALUES (?,?,?,?,?,'PROVISIONING',?,?,?,?,?,1,?,?)",
            (iid, op_id, PROBE_POOL, provider, label, now, now, now + max_s + 300, now, now,
             now, now),
        )  # fmt: skip
        conn.execute(
            "INSERT INTO transitions(instance_id, ts, from_state, to_state, reason, actor)"
            " VALUES (?,?,'STOPPED','PROVISIONING','probe','admin')",
            (iid, now),
        )
    return iid


async def _sleep(clock: Clock, s: float) -> None:
    await clock.sleep_until(clock.now() + s)


async def lifecycle_probe(
    conn: sqlite3.Connection,
    adapter: Any,
    secret: bytes,
    clock: Clock,
    cfg: Config,
    *,
    image: str,
    gpu_type_id: str,
    disk_gb: int = 20,
    max_price_usd_h: float = 0.30,
    max_s: float = 480.0,
    poll_s: float = 5.0,
    hold_s: float = PROBE_HOLD_S,
    start_cmd: Sequence[str] = PROBE_CMD,
) -> ProbeReport:
    """`adapter` is the raw RunpodAdapter (it takes `docker_start_cmd`). The pod is kept
    `hold_s` after the create answer (capped by `max_s`)."""
    if hold_s < 0:
        raise OpsError("--hold-s must be >= 0")
    if max_s > cfg.timeouts.provisioning_s - 60:
        raise OpsError("--max-s must stay 60 s below timeouts.provisioning_s")
    op_id = new_op_id()
    label = make_label(op_id, secret)
    t0 = clock.now()
    iid = _insert_probe_row(conn, adapter.name, op_id, label, t0, max_s)
    rep = ProbeReport(label=label, instance_id=iid)
    spec = InstanceSpec(
        pool=PROBE_POOL, offer_id=gpu_type_id, image=image, disk_gb=disk_gb,
        max_price_usd_h=max_price_usd_h, max_lifetime_s=int(max_s),
        env={"LLMHUB_TRANSPORT": "proxy", "LLMHUB_PORT": "8080", "LLMHUB_OP_ID": op_id},
    )  # fmt: skip
    try:
        try:
            res = await adapter.create(spec, label, docker_start_cmd=list(start_cmd))
        except CreatedResourceRejected as exc:  # REV-S10: ref to the DB first, teardown next
            rep.error = exc.err.message
            rep.ref = exc.resource.ref
            _store_rejected(conn, iid, exc, clock.now())
            return rep
        except ProviderError as exc:
            rep.error = exc.err.message
            if exc.err.outcome_unknown:
                rep.notes.append("create outcome unknown: looking the label up")
                res = None
            else:
                return rep
        rep.create_s = round(clock.now() - t0, 1)
        t_created = clock.now()
        if res is not None:
            rep.ref, rep.name_ok, rep.cost_per_hr = res.ref, res.label == label, res.price_usd_h
            with tx(conn):
                conn.execute(
                    "UPDATE instances SET provider_ref=?, host_id=?, price_usd_h=?,"
                    " endpoint=? WHERE id=?",
                    (res.ref, res.host_id, res.price_usd_h, res.endpoint, iid),
                )
        while clock.now() - t0 < max_s:
            owned = [r for r in await adapter.list_owned(label) if r.label == label]
            if owned and rep.visible_s is None:
                rep.visible_s = round(clock.now() - t0, 1)
                if rep.ref is None:
                    rep.ref = owned[0].ref
            mine = next((r for r in owned if r.ref == rep.ref), None)
            if (mine is not None and mine.status == RemoteStatus.RUNNING
                    and rep.desired_running_s is None):  # fmt: skip
                rep.desired_running_s = round(clock.now() - t0, 1)
            if rep.desired_running_s is not None and clock.now() - t_created >= hold_s:
                break
            await _sleep(clock, poll_s)
        else:
            rep.notes.append("max-s reached before RUNNING" if rep.desired_running_s is None
                             else "max-s reached before the end of the hold")  # fmt: skip
        rep.held_s = round(clock.now() - t_created, 1)
    finally:
        await _probe_teardown(conn, adapter, rep, clock, label, poll_s)
    return rep


def _store_rejected(conn: sqlite3.Connection, iid: str, exc: CreatedResourceRejected,
                    now: float) -> None:  # fmt: skip
    from llmhub.lifecycle_manager.safety import record_incident

    res = exc.resource
    with tx(conn):
        conn.execute("UPDATE instances SET provider_ref=?, host_id=?, price_usd_h=? WHERE id=?",
                     (res.ref, res.host_id, res.price_usd_h, iid))  # fmt: skip
        record_incident(
            conn, now, "created_resource_rejected", "critical", ref=iid,
            dedup_key=f"created_resource_rejected:{res.ref}",
            details={"provider_ref": res.ref, "label": res.label[:80], "what": exc.what[:80]},
        )  # fmt: skip


async def _probe_teardown(conn: sqlite3.Connection, adapter: Any, rep: ProbeReport,
                          clock: Clock, label: str, poll_s: float) -> None:  # fmt: skip
    """Destroy every pod with the probe label, then wait (<= 180 s) for two empty lists
    `list_staleness_s` apart. The row goes to ERROR: the controller confirms zero itself."""
    t1 = clock.now()
    stale = float(getattr(adapter, "list_staleness_s", 0.0) or 0.0)
    first_zero: float | None = None
    while clock.now() - t1 < 180:
        if rep.ref and not rep.destroyed:  # R2-2: the known ref never waits for a listing
            await _probe_destroy(adapter, rep, rep.ref)
        try:
            owned = [r for r in await adapter.list_owned(label) if r.label == label]
        except ProviderError as exc:
            rep.notes.append(f"list failed: {exc.err.message}")
            await _sleep(clock, poll_s)
            continue
        for ref in sorted({r.ref for r in owned}):
            await _probe_destroy(adapter, rep, ref)
        if not owned:
            if first_zero is None:
                first_zero = clock.now()
            if clock.now() - first_zero >= stale:
                rep.zero = True
                rep.destroy_gone_s = round(first_zero - t1, 1)
                break
        else:
            first_zero = None
        await _sleep(clock, poll_s)
    from llmhub.lifecycle_manager.machine import transition
    from llmhub.lifecycle_manager.states import State

    with tx(conn):
        transition(conn, rep.instance_id, State.PROVISIONING, State.ERROR, "E3", clock.now(),
                   "admin", last_error="probe_done", reason="probe_done")  # fmt: skip


async def _probe_destroy(adapter: Any, rep: ProbeReport, ref: str) -> None:
    try:
        await adapter.destroy(ref)
        rep.destroyed = True
    except ProviderError as exc:
        rep.notes.append(f"destroy {ref}: {exc.err.message}")


# ------------------------------------------------------------------ D3 scan-secrets
GENERIC_PATTERNS = (
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bghp_[A-Za-z0-9]{20,}", r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bhf_[A-Za-z0-9]{20,}", r"\brpa_[A-Za-z0-9]{16,}", r"\blh_[A-Za-z0-9_-]{16,}",
    r"\bsk-[A-Za-z0-9_-]{16,}", r"LLMHUB_[A-Z_]*SECRET=\S+",
)  # fmt: skip


def scan_secrets(text: str, op_id: str, exact: Sequence[str]) -> int:
    """Number of secret-looking hits: exact values (the op's API key, provider keys), the op's
    weights token shape and generic key patterns. Only the count leaves this function."""
    if not re.fullmatch(r"[0-9a-f]{12}", op_id):
        raise OpsError("--op must be 12 hex chars")
    hits = sum(text.count(v) for v in exact if v)
    hits += len(re.findall(rf"v1\.{op_id}\.[0-9]{{1,12}}\.[A-Za-z0-9_-]{{43}}", text))
    hits += sum(len(re.findall(p, text)) for p in GENERIC_PATTERNS)
    return hits


# ------------------------------------------------------------------ D3 weights-token-check
def weights_token_for(conn: sqlite3.Connection, cfg: Config, secret: bytes, op_id: str) -> str:
    from llmhub.cloud_manager.instance_env import weights_token_exp
    from llmhub.weights.tokens import issue_token, token_expiry

    row = conn.execute("SELECT created_at FROM instances WHERE op_id=?", (op_id,)).fetchone()
    if row is None:
        raise OpsError(f"no instance with op_id {op_id}")
    created = float(row["created_at"])
    exp = weights_token_exp(created, token_expiry(created, cfg.timeouts), cfg)
    return issue_token(secret, op_id, exp)


def expected_weights_verdict(conn: sqlite3.Connection, secret: bytes, token: str, url: str,
                             now: float) -> str:  # fmt: skip
    """The verdict `check_request` gives the token for this URL with the DB state now. Not a
    proof (the server also checks IP, volume, path tables): that is the weights-auth log."""
    from urllib.parse import urlsplit

    from llmhub.weights.tokens import check_request, split_uri

    path = urlsplit(url).path
    parts = split_uri(path)
    allowed = frozenset({parts[1]}) if parts else frozenset[str]()

    def state_of(op: str) -> str | None:
        row = conn.execute("SELECT state FROM instances WHERE op_id=?", (op,)).fetchone()
        return None if row is None else str(row[0])

    verdict, _ = check_request(secret, f"Bearer {token}", path, now, state_of, allowed)
    return verdict.value


def default_weights_url(cfg: Config, op_id: str) -> tuple[str, Path | None]:
    for src in cfg.weights.sources:
        if src.kind == "self_hosted":
            rel = src.paths.get("model") or next(iter(src.paths.values()))
            return src.base_url.replace("{op_id}", op_id).rstrip("/") + "/" + rel, src.ca_pem_file
    raise OpsError("no self_hosted weights source in the config")


def weights_status(url: str, token: str, ca: Path | None, *, transport: Any = None) -> int:
    """Status of a 1-byte Range GET with the token (nothing of the body is read)."""
    import ssl

    import httpx

    verify: Any = True if ca is None else ssl.create_default_context(cafile=str(ca))
    headers = {"Authorization": f"Bearer {token}", "Range": "bytes=0-0"}
    with (
        httpx.Client(verify=verify, timeout=15, transport=transport, trust_env=False) as c,
        c.stream("GET", url, headers=headers) as r,
    ):
        return r.status_code


__all__ = [
    "PROBE_POOL", "ExcludeResult", "OpsError", "ProbeReport", "default_weights_url",
    "expected_weights_verdict",
    "ledger_exclude", "lifecycle_probe", "provider_check", "scan_secrets", "weights_status",
    "weights_token_for",
]  # fmt: skip
