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
- `run` (fix round 1, F1/F2): the request always goes to the pod with `stream: true`
  (+ `stream_options.include_usage`) and the SSE answer is assembled into one
  `chat.completion` JSON. Bytes flow during generation, so the pause between bytes
  (`read_s`) is meaningful and a proxy's ~100 s limit (Cloudflare/RunPod 524) is not hit.
- `run` error mapping (checked against `Controller._run_job`):
  `NetworkLost`  - connect/TLS errors and drops BEFORE the response headers, 502/503/504/52x
                   without our front's JSON (a proxy without a pod), our front's 503
                   {"error":"restarting"} (llama-server's single restart, F9); the core bounds
                   repeated `NetworkLost` of one job on one instance;
  `JobRunError`  - (counts an attempt) the job cap, a pause > `read_s` after the headers, a drop
                   after the headers, a truncated SSE stream;
  `InstanceLost` - our front says failed (500 {"status":"failed"}), loading / not ready (503:
                   the container restarted), llama unreachable (502 {"error":"upstream
                   unavailable"}), 401 (not our pod behind this endpoint, logged high);
  `JobRunResult(finish_reason="error", body=...)` - 4xx and llama-server's own JSON errors.
  No retries inside (the core requeues); `CancelledError` propagates.
- Clients of instances that left TALK_STATES are closed on the next call (F8).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
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


class JobRunError(Exception):
    """The job failed on a reachable pod after the request was accepted (cap, stall, drop,
    truncated stream). A plain exception for the core: requeue WITH the attempt counted."""


class _BeforeHeaders(Exception):
    """Transport failure before the response headers (-> NetworkLost)."""


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


# --- streaming ---------------------------------------------------------------------------
_STREAM_OPTS = {"include_usage": True}
_SSE_EOL = re.compile(r"\r\n|\r|\n")


def force_stream(body: bytes) -> bytes:
    """The job body with `stream: true` (the pod streams; we assemble). Not JSON -> unchanged."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(data, dict):
        return body
    data["stream"] = True
    opts = data.get("stream_options")
    data["stream_options"] = {**opts, **_STREAM_OPTS} if isinstance(opts, dict) else _STREAM_OPTS
    return json.dumps(data, ensure_ascii=False).encode()


def _merge_tool_calls(acc: list[dict[str, Any]], deltas: object) -> None:
    if not isinstance(deltas, list):
        return
    for d in deltas:
        if not isinstance(d, dict):
            continue
        idx = d.get("index", len(acc))
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < 1024:
            continue
        while len(acc) <= idx:
            acc.append({"function": {"name": "", "arguments": ""}})
        cur = acc[idx]
        for k in ("id", "type"):
            if isinstance(d.get(k), str):
                cur[k] = d[k]
        fn = d.get("function")
        if isinstance(fn, dict):
            for k in ("name", "arguments"):
                if isinstance(fn.get(k), str):
                    cur["function"][k] += fn[k]


def assemble_sse(content: bytes) -> tuple[dict[str, Any] | None, bool]:
    """llama-server SSE (`data: {...}` ... `data: [DONE]`) -> (chat.completion dict, complete).

    `complete` is False when the stream ended without `[DONE]` and without a finish_reason
    (a drop). An `error` event -> ({"error": ...}, True). Never raises.
    """
    try:
        text = content.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - tolerant by contract
        return None, False
    out: dict[str, Any] = {"object": "chat.completion"}
    choices: dict[int, dict[str, Any]] = {}
    done = False
    finished = False
    seen = False
    # SSE lines end with CRLF/LF/CR only; str.splitlines() would also split on U+2028/U+2029/
    # U+0085, which llama-server's JSON leaves raw inside strings (review r2)
    for raw_line in _SSE_EOL.split(text):
        line = raw_line.strip()
        if line.startswith("error:"):
            err = _json(line[6:].strip().encode())
            return {"error": err if err is not None else line[6:].strip()}, True
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            done = True
            break
        ev = _json(payload.encode())
        if not isinstance(ev, dict):
            continue
        seen = True
        if "error" in ev and "choices" not in ev:
            return {"error": ev["error"]}, True
        for k in ("id", "created", "model", "system_fingerprint"):
            if k in ev:
                out[k] = ev[k]
        for k in ("usage", "timings"):
            if isinstance(ev.get(k), dict):
                out[k] = ev[k]
        evc = ev.get("choices")
        if not isinstance(evc, list):
            continue
        for ch in evc:
            if not isinstance(ch, dict):
                continue
            idx = ch.get("index", 0)
            if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < 64:
                continue
            cur = choices.setdefault(
                idx, {"index": idx, "message": {"role": "assistant", "content": ""},
                      "finish_reason": None, "_tools": []},
            )  # fmt: skip
            delta = ch.get("delta")
            if isinstance(delta, dict):
                msg = cur["message"]
                if isinstance(delta.get("role"), str):
                    msg["role"] = delta["role"]
                for k in ("content", "reasoning_content"):
                    if isinstance(delta.get(k), str):
                        msg[k] = msg.get(k, "") + delta[k]
                _merge_tool_calls(cur["_tools"], delta.get("tool_calls"))
            fr = ch.get("finish_reason")
            if isinstance(fr, str) and fr:
                cur["finish_reason"] = fr
                finished = True
    if not seen:
        return None, False
    out_choices = []
    for idx in sorted(choices):
        cur = choices[idx]
        tools = cur.pop("_tools")
        if tools:
            cur["message"]["tool_calls"] = tools
        out_choices.append(cur)
    out["choices"] = out_choices
    return out, done or finished


CHARS_PER_TOKEN = 4.0  # rough estimate for answers without usage/timings (R2-7)


def _estimate_tokens_out(data: Mapping[str, Any], max_tokens: int) -> int:
    """ceil(answer chars / CHARS_PER_TOKEN), at least 1 for a non-empty answer, at most
    `max_tokens` (when positive). Counts content, reasoning and tool call names/arguments."""
    chars = 0
    choices = data.get("choices")
    for ch in choices if isinstance(choices, list) else []:
        msg = ch.get("message") if isinstance(ch, dict) else None
        if not isinstance(msg, dict):
            continue
        for k in ("content", "reasoning_content"):
            v = msg.get(k)
            if isinstance(v, str):
                chars += len(v)
        tools = msg.get("tool_calls")
        for tc in tools if isinstance(tools, list) else []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            if isinstance(fn, dict):
                for k in ("name", "arguments"):
                    v = fn.get(k)
                    if isinstance(v, str):
                        chars += len(v)
    est = math.ceil(chars / CHARS_PER_TOKEN)
    return min(est, max_tokens) if max_tokens > 0 else est


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

    async def _prune(self) -> None:
        """F8: close clients of instances that left TALK_STATES (or vanished)."""
        if not self._clients:
            return
        ids = sorted({iid for _, iid in self._clients})
        marks = ",".join("?" for _ in ids)
        states = ",".join("?" for _ in TALK_STATES)
        live = {
            r["id"]
            for r in self.conn.execute(
                f"SELECT id FROM instances WHERE id IN ({marks}) AND state IN ({states})",
                (*ids, *TALK_STATES),
            )
        }
        dead = [k for k in self._clients if k[1] not in live]
        for k in dead:
            _, client = self._clients.pop(k)
            await client.aclose()

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

    async def _timeouts(self, endpoint: str) -> Any:
        await self._prune()
        try:
            return self._target(endpoint).pool.infer.client
        except _Unusable:
            return None

    # --- InferenceBackend ------------------------------------------------------------------
    async def ping(self, endpoint: str) -> bool:
        c = await self._timeouts(endpoint)
        if c is None:
            return False
        resp = await self._safe(endpoint, "GET", "/ping", c.ping_s, auth=False)
        return resp is not None and resp.status_code == 204

    async def health(self, endpoint: str) -> bool:
        c = await self._timeouts(endpoint)
        if c is None:
            return False
        resp = await self._safe(endpoint, "GET", "/health", c.health_s)
        if resp is None or resp.status_code != 200:
            return False
        body = _json(resp.content)
        return isinstance(body, dict) and body.get("status") == "ok"

    async def test_request(self, endpoint: str) -> bool:
        c = await self._timeouts(endpoint)
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
        c = await self._timeouts(endpoint)
        if c is None:
            return None
        resp = await self._safe(endpoint, "GET", "/health", c.health_s)
        if resp is None or resp.status_code == 401:
            return None
        return parse_load_status(_json(resp.content))

    async def run(self, endpoint: str, job: JobRunSpec) -> JobRunResult:
        await self._prune()
        try:
            t = self._target(endpoint)
        except _Unusable:
            raise NetworkLost("no unique live instance for the endpoint") from None
        c = t.pool.infer.client
        prompt_s = job.tokens_in / t.pool.pp_tps
        cap = (prompt_s + job.max_tokens / t.pool.tg_tps) * c.job_cap_factor + JOB_CAP_BASE_S
        # the first byte may wait for prompt processing; later bytes only for one token
        first_byte_s = c.read_s + prompt_s * c.job_cap_factor
        client = await self._client(endpoint, t)
        headers = {"Authorization": f"Bearer {t.api_key}", "Content-Type": "application/json"}
        req = client.build_request(
            "POST", t.base_url + "/v1/chat/completions", headers=headers,
            content=force_stream(job.body),
            # read=None: the header wait is bounded by the cap, pauses by our own timers
            timeout=httpx.Timeout(None, connect=c.connect_s),
        )  # fmt: skip
        try:
            async with asyncio.timeout(cap):
                status, ctype, content = await self._send(client, req, t, first_byte_s, c.read_s)
        except _PinMismatch:
            self._pin_failed(endpoint)
            raise NetworkLost("tls pin mismatch") from None
        except _BeforeHeaders as exc:
            cause = exc.__cause__
            if t.direct and cause is not None and _is_cert_error(cause):
                self._pin_failed(endpoint)
            raise NetworkLost(str(exc)) from None
        except TimeoutError:  # the job cap, or a pause after the headers: counts an attempt
            raise JobRunError("timeout") from None
        except (httpx.HTTPError, OSError) as exc:  # a drop after the headers
            raise JobRunError(type(exc).__name__) from None
        if status == 200 and "text/event-stream" in ctype:
            data, complete = assemble_sse(content)
            if data is None or not complete:
                raise JobRunError("truncated stream")
            if "error" in data and "choices" not in data:
                return JobRunResult(job.tokens_in, 0, "error", json.dumps(data).encode())
            content = json.dumps(data, ensure_ascii=False).encode()
        return self._result(job, status, content)

    async def _send(
        self,
        client: httpx.AsyncClient,
        req: httpx.Request,
        t: _Target,
        first_byte_s: float,
        read_s: float,
    ) -> tuple[int, str, bytes]:
        try:
            resp = await client.send(req, stream=True)  # header wait: bounded by the job cap
        except (httpx.HTTPError, OSError) as exc:
            raise _BeforeHeaders(type(exc).__name__) from exc
        try:
            self._check_pin(t, resp)
            chunks: list[bytes] = []
            it = resp.aiter_raw().__aiter__()
            pause = first_byte_s
            while True:
                try:
                    async with asyncio.timeout(pause):  # pause between bytes
                        chunk = await it.__anext__()
                except StopAsyncIteration:
                    break
                chunks.append(chunk)
                pause = read_s
            return resp.status_code, resp.headers.get("content-type", ""), b"".join(chunks)
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
        if ours and status == 503 and data.get("error") == "restarting":
            raise NetworkLost("pod restarting llama-server")  # F9: bounded by the core
        if (
            ours
            and status == 503
            and (data.get("status") == "loading" or data.get("error") == "not ready")
        ):
            raise InstanceLost("pod restarted")
        if ours and status == 502 and data.get("error") == "upstream unavailable":
            raise InstanceLost("llama-server unreachable")
        if status in (502, 503, 504, 520, 521, 522, 523, 524):  # a proxy without a pod
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
        # R2-7: no usage and no timings (e.g. the stream ended after finish_reason, before the
        # usage chunk): estimate from the answer length instead of billing 0 output tokens
        est = _estimate_tokens_out(data, job.max_tokens)
        log.warning("answer without usage/timings: tokens_out estimated as %d", est)
        return job.tokens_in, est

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
