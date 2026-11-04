"""Cloud `InferenceBackend`: httpx client controller -> `llmhub-infer` pod (T-370 stream G).

Implements the FROZEN `InferenceBackend` protocol and the optional `LoadStatusSource`.

- Credentials come from the instance ROW, not from the endpoint string: the endpoint is mapped
  to exactly one live row; 0 or > 1 rows -> False / `NetworkLost` (> 1 is logged as high).
  The API key and the `direct` certificate are derived from `op_id` (`instance_env`).
- TLS `direct`: the derived certificate is the only trust anchor (pin, no system CAs, no
  hostname check); after the handshake sha256(DER) of the peer certificate is compared with
  `instances.tls_fingerprint`. A mismatch (or a certificate the pin rejects) -> False /
  `NetworkLost` and `on_pin_mismatch(endpoint)` (the composition root writes a high incident).
  `proxy`: system CAs, `https://` only, the per-instance key.
- `run` error mapping (plan 5.3, checked against `Controller._run_job` in G1):
  `NetworkLost`  - connect/read timeouts, transport errors, TLS errors, the job cap, 502/503/504
                   without our front's JSON (a RunPod proxy without a pod);
  `InstanceLost` - our front says failed (500 {"status":"failed"}), loading / not ready (503:
                   the container restarted), llama unreachable (502 {"error":"upstream
                   unavailable"}), 401 (not our pod behind this endpoint, logged high);
  `JobRunResult(finish_reason="error", body=...)` - 4xx and llama-server's own JSON errors.
  No retries inside (the core requeues); `CancelledError` propagates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import sqlite3
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from llmhub.clock import Clock
from llmhub.cloud_manager.inference import InstanceLost, JobRunResult, JobRunSpec, NetworkLost
from llmhub.cloud_manager.instance_env import InstanceSecrets, derive_api_key, derive_tls
from llmhub.cloud_manager.load_status import LoadStatus, parse_load_status
from llmhub.configuration.models import Config, PoolConfig
from llmhub.lifecycle_manager.safety import record_incident

log = logging.getLogger("llmhub.http_inference")

# states in which the controller talks to the pod behind `instances.endpoint`
TALK_STATES = ("BOOTING", "LOADING_MODEL", "READY", "BUSY", "IDLE", "STOPPING")
JOB_CAP_BASE_S = 30.0
TEST_BODY = {
    "messages": [{"role": "user", "content": "Reply with: ok"}],
    "max_tokens": 8,
    "temperature": 0,
    "stream": False,
}

TransportFactory = Callable[[ssl.SSLContext], httpx.AsyncBaseTransport]


class _PinMismatch(Exception):
    pass


class _Unusable(Exception):
    """No (or no unique) instance row for the endpoint, or a non-https endpoint."""


# --- timings -------------------------------------------------------------------------------
@dataclass(frozen=True)
class Timings:
    prompt_n: int
    prompt_ms: float
    predicted_n: int
    predicted_ms: float
    draft_n: int | None = None
    draft_n_accepted: int | None = None

    @property
    def pp_tps(self) -> float:
        return self.prompt_n / (self.prompt_ms / 1000) if self.prompt_ms > 0 else 0.0

    @property
    def tg_tps(self) -> float:
        return self.predicted_n / (self.predicted_ms / 1000) if self.predicted_ms > 0 else 0.0

    @property
    def accept_rate(self) -> float | None:
        if self.draft_n is None or self.draft_n_accepted is None or self.draft_n <= 0:
            return None
        return min(1.0, self.draft_n_accepted / self.draft_n)


def _nonneg(v: object, kind: type[int | float]) -> int | float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    if kind is int:
        return int(v) if float(v).is_integer() else None
    return float(v)


def parse_timings(body: Mapping[str, Any] | object) -> Timings | None:
    """llama-server `timings` (non-stream JSON) -> Timings. Never raises; garbage -> None."""
    try:
        if not isinstance(body, Mapping):
            return None
        t = body.get("timings")
        if not isinstance(t, Mapping):
            return None
        pn = _nonneg(t.get("prompt_n"), int)
        pm = _nonneg(t.get("prompt_ms"), float)
        gn = _nonneg(t.get("predicted_n"), int)
        gm = _nonneg(t.get("predicted_ms"), float)
        if pn is None or pm is None or gn is None or gm is None:
            return None
        dn = _nonneg(t.get("draft_n"), int)
        da = _nonneg(t.get("draft_n_accepted"), int)
        return Timings(
            prompt_n=int(pn), prompt_ms=float(pm), predicted_n=int(gn), predicted_ms=float(gm),
            draft_n=None if dn is None else int(dn),
            draft_n_accepted=None if da is None or dn is None else int(da),
        )  # fmt: skip
    except Exception:  # noqa: BLE001 - tolerant by contract
        return None


# --- backend -------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Target:
    iid: str
    state: str
    pool: PoolConfig
    base_url: str
    api_key: str
    direct: bool
    fingerprint: str | None


def _is_cert_error(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _peer_fingerprint(resp: httpx.Response) -> str | None:
    stream = resp.extensions.get("network_stream")
    if stream is None:
        return None
    try:
        sslobj = stream.get_extra_info("ssl_object")
    except Exception:  # noqa: BLE001 - backend-specific stream
        return None
    if sslobj is None:
        return None
    der = sslobj.getpeercert(binary_form=True)
    return hashlib.sha256(der).hexdigest() if der else None


def _json(content: bytes) -> Any:
    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError):
        return None


class HttpInferenceBackend:
    def __init__(
        self,
        conn: sqlite3.Connection,
        secrets: InstanceSecrets,
        cfg: Config,
        *,
        transport_factory: TransportFactory | None = None,
        on_timings: Callable[[str, Timings], None] | None = None,
        on_pin_mismatch: Callable[[str], None] | None = None,
        proxy_ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.conn = conn
        self.secrets = secrets
        self.cfg = cfg
        self._factory: TransportFactory = transport_factory or (
            lambda ctx: httpx.AsyncHTTPTransport(verify=ctx)
        )
        self.on_timings = on_timings
        self.on_pin_mismatch = on_pin_mismatch
        self._proxy_ctx = proxy_ssl_context
        self._clients: dict[tuple[str, str], tuple[str, httpx.AsyncClient]] = {}

    # --- lookup / clients ------------------------------------------------------------------
    def _target(self, endpoint: str) -> _Target:
        marks = ",".join("?" for _ in TALK_STATES)
        rows = self.conn.execute(
            "SELECT id, op_id, created_at, pool, state, tls_fingerprint FROM instances"
            f" WHERE endpoint=? AND state IN ({marks})",
            (endpoint, *TALK_STATES),
        ).fetchall()
        if len(rows) != 1:
            if len(rows) > 1:
                log.error("endpoint maps to %d live instances; refusing to talk", len(rows))
            raise _Unusable
        row = rows[0]
        pool = self.cfg.pools.get(row["pool"])
        if pool is None or not endpoint.startswith("https://"):
            raise _Unusable
        return _Target(
            iid=row["id"], state=row["state"], pool=pool, base_url=endpoint.rstrip("/"),
            api_key=derive_api_key(self.secrets.instance, row["op_id"]),
            direct=pool.infer.transport == "direct", fingerprint=row["tls_fingerprint"],
        )  # fmt: skip

    def _ssl_context(self, t: _Target) -> ssl.SSLContext:
        if not t.direct:
            return self._proxy_ctx or ssl.create_default_context()
        row = self.conn.execute(
            "SELECT op_id, created_at FROM instances WHERE id=?", (t.iid,)
        ).fetchone()
        cert_pem = derive_tls(self.secrets.instance, row["op_id"], float(row["created_at"]))[0]
        ctx = ssl.create_default_context(cadata=cert_pem)  # the only anchor: no system CAs
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx

    async def _client(self, endpoint: str, t: _Target) -> httpx.AsyncClient:
        key = (endpoint, t.iid)
        cached = self._clients.get(key)
        if cached is not None and cached[0] == t.state:
            return cached[1]
        if cached is not None:  # the state changed: fresh connections
            await cached[1].aclose()
        c = self.cfg.pools[t.pool.name].infer.client
        client = httpx.AsyncClient(
            transport=self._factory(self._ssl_context(t)),
            timeout=httpx.Timeout(c.read_s, connect=c.connect_s),
            follow_redirects=False,
        )
        self._clients[key] = (t.state, client)
        return client

    def _pin_failed(self, endpoint: str) -> None:
        log.error("TLS pin mismatch for an instance endpoint")
        if self.on_pin_mismatch is not None:
            try:
                self.on_pin_mismatch(endpoint)
            except Exception:
                log.exception("on_pin_mismatch failed")

    def _check_pin(self, t: _Target, resp: httpx.Response) -> None:
        if not t.direct:
            return
        peer = _peer_fingerprint(resp)
        if peer is None or t.fingerprint is None or peer != t.fingerprint:
            raise _PinMismatch

    async def _request(
        self,
        endpoint: str,
        method: str,
        path: str,
        *,
        timeout_s: float,
        auth: bool = True,
        body: bytes | None = None,
    ) -> tuple[_Target, httpx.Response]:
        """One short request (ping/health/test/load_status), body read, total time capped."""
        t = self._target(endpoint)
        client = await self._client(endpoint, t)
        headers = {"Authorization": f"Bearer {t.api_key}"} if auth else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        connect = t.pool.infer.client.connect_s
        try:
            async with asyncio.timeout(timeout_s + connect):
                resp = await client.request(
                    method, t.base_url + path, headers=headers, content=body,
                    timeout=httpx.Timeout(timeout_s, connect=connect),
                )  # fmt: skip
        except httpx.TransportError as exc:
            if t.direct and _is_cert_error(exc):
                self._pin_failed(endpoint)
                raise _PinMismatch from None
            raise
        try:
            self._check_pin(t, resp)
        except _PinMismatch:
            self._pin_failed(endpoint)
            raise
        return t, resp

    async def _safe(
        self, endpoint: str, method: str, path: str, timeout_s: float, **kw: Any
    ) -> httpx.Response | None:
        try:
            return (await self._request(endpoint, method, path, timeout_s=timeout_s, **kw))[1]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - every failure of a probe is just "no"
            return None

    def _timeouts(self, endpoint: str) -> Any:
        try:
            return self._target(endpoint).pool.infer.client
        except _Unusable:
            return None

    # --- InferenceBackend ------------------------------------------------------------------
    async def ping(self, endpoint: str) -> bool:
        c = self._timeouts(endpoint)
        if c is None:
            return False
        resp = await self._safe(endpoint, "GET", "/ping", c.ping_s, auth=False)
        return resp is not None and resp.status_code == 204

    async def health(self, endpoint: str) -> bool:
        c = self._timeouts(endpoint)
        if c is None:
            return False
        resp = await self._safe(endpoint, "GET", "/health", c.health_s)
        if resp is None or resp.status_code != 200:
            return False
        body = _json(resp.content)
        return isinstance(body, dict) and body.get("status") == "ok"

    async def test_request(self, endpoint: str) -> bool:
        c = self._timeouts(endpoint)
        if c is None:
            return False
        body = json.dumps(TEST_BODY).encode()
        resp = await self._safe(endpoint, "POST", "/v1/chat/completions", c.test_s, body=body)
        if resp is None or resp.status_code != 200:
            return False
        data = _json(resp.content)
        choices = data.get("choices") if isinstance(data, dict) else None
        return isinstance(choices, list) and len(choices) > 0

    async def load_status(self, endpoint: str) -> LoadStatus | None:
        c = self._timeouts(endpoint)
        if c is None:
            return None
        resp = await self._safe(endpoint, "GET", "/health", c.health_s)
        if resp is None or resp.status_code == 401:
            return None
        return parse_load_status(_json(resp.content))

    async def run(self, endpoint: str, job: JobRunSpec) -> JobRunResult:
        try:
            t = self._target(endpoint)
        except _Unusable:
            raise NetworkLost("no unique live instance for the endpoint") from None
        c = t.pool.infer.client
        cap = (
            job.tokens_in / t.pool.pp_tps + job.max_tokens / t.pool.tg_tps
        ) * c.job_cap_factor + JOB_CAP_BASE_S
        client = await self._client(endpoint, t)
        headers = {"Authorization": f"Bearer {t.api_key}", "Content-Type": "application/json"}
        req = client.build_request(
            "POST", t.base_url + "/v1/chat/completions", headers=headers, content=job.body
        )
        try:
            async with asyncio.timeout(cap):
                status, content = await self._send(client, req, t, c.read_s, c.connect_s)
        except _PinMismatch:
            self._pin_failed(endpoint)
            raise NetworkLost("tls pin mismatch") from None
        except TimeoutError:
            raise NetworkLost("timeout") from None
        except httpx.HTTPError as exc:
            if t.direct and _is_cert_error(exc):
                self._pin_failed(endpoint)
            raise NetworkLost(type(exc).__name__) from None
        except OSError as exc:  # raw socket / TLS errors (ssl.SSLError is an OSError)
            raise NetworkLost(type(exc).__name__) from None
        return self._result(job, status, content)

    async def _send(
        self,
        client: httpx.AsyncClient,
        req: httpx.Request,
        t: _Target,
        read_s: float,
        connect_s: float,
    ) -> tuple[int, bytes]:
        async with asyncio.timeout(read_s + connect_s):  # until the response headers
            resp = await client.send(req, stream=True)
        try:
            self._check_pin(t, resp)
            chunks: list[bytes] = []
            it = resp.aiter_raw().__aiter__()
            while True:
                try:
                    async with asyncio.timeout(read_s):  # pause between bytes
                        chunk = await it.__anext__()
                except StopAsyncIteration:
                    break
                chunks.append(chunk)
            return resp.status_code, b"".join(chunks)
        finally:
            await resp.aclose()

    def _result(self, job: JobRunSpec, status: int, content: bytes) -> JobRunResult:
        data = _json(content)
        ours = isinstance(data, dict)
        if status == 401:
            log.error("pod rejected the instance key (not our pod behind the endpoint?)")
            raise InstanceLost("unauthorized")
        if ours and status == 500 and data.get("status") == "failed":
            raise InstanceLost("pod failed")
        if (
            ours
            and status == 503
            and (data.get("status") == "loading" or data.get("error") == "not ready")
        ):
            raise InstanceLost("pod restarted")
        if ours and status == 502 and data.get("error") == "upstream unavailable":
            raise InstanceLost("llama-server unreachable")
        if status in (502, 503, 504):  # not our front's JSON: a proxy without a pod
            raise NetworkLost(f"http {status}")
        if status != 200 or not ours:
            return JobRunResult(job.tokens_in, 0, "error", content)
        timings = parse_timings(data)
        if timings is not None and self.on_timings is not None:
            try:
                self.on_timings(job.job_id, timings)
            except Exception:
                log.exception("on_timings failed")
        tin, tout = self._tokens(data, timings, job)
        finish = "stop"
        choices = data.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            fr = choices[0].get("finish_reason")
            if isinstance(fr, str) and fr:
                finish = fr
        return JobRunResult(tokens_in=tin, tokens_out=tout, finish_reason=finish, body=content)

    @staticmethod
    def _tokens(data: dict[str, Any], t: Timings | None, job: JobRunSpec) -> tuple[int, int]:
        usage = data.get("usage")
        if isinstance(usage, dict):
            pin = _nonneg(usage.get("prompt_tokens"), int)
            pout = _nonneg(usage.get("completion_tokens"), int)
            if pin is not None and pout is not None:
                return int(pin), int(pout)
        if t is not None:
            return t.prompt_n, t.predicted_n
        return job.tokens_in, 0

    async def aclose(self) -> None:
        clients = [c for _, c in self._clients.values()]
        self._clients.clear()
        for c in clients:
            await c.aclose()


def pin_incident_recorder(conn: sqlite3.Connection, clock: Clock) -> Callable[[str], None]:
    """`on_pin_mismatch` -> incident `tls_pin_mismatch` (high), at most one per endpoint-hour."""

    def record(endpoint: str) -> None:
        now = clock.now()
        row = conn.execute(
            "SELECT id FROM instances WHERE endpoint=? ORDER BY created_at DESC LIMIT 1",
            (endpoint,),
        ).fetchone()
        record_incident(
            conn, now, "tls_pin_mismatch", "high", ref=None if row is None else row["id"],
            dedup_key=f"tls_pin:{endpoint}:{int(now // 3600)}",
            details={"endpoint": endpoint},
        )  # fmt: skip

    return record


def make_cloud_inference(
    conn: sqlite3.Connection,
    secrets: InstanceSecrets,
    cfg: Config,
    clock: Clock,
    *,
    on_timings: Callable[[str, Timings], None] | None = None,
) -> HttpInferenceBackend:
    """Composition-root factory for real providers (Mock pools keep `MockInference`)."""
    return HttpInferenceBackend(
        conn, secrets, cfg, on_timings=on_timings,
        on_pin_mismatch=pin_incident_recorder(conn, clock),
    )  # fmt: skip
