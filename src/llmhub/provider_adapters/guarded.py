"""GuardedAdapter: timeouts, retries and a circuit breaker around any ProviderAdapter.

- never called inside a DB transaction (`assert_not_in_tx`);
- every call has a hard timeout (`retry.call_timeout_s`) measured on the injected clock;
- idempotent reads (`get`, `list_owned`, `balance`, `billing_since`) are retried with backoff;
- `create` and `destroy` are never retried here; a `create` timeout is an unknown outcome;
- the breaker gates only `create` and `list_offers`; teardown and reads always pass;
- errors of actor "reaper" never open the controller's breaker.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Literal

from llmhub.clock import Clock
from llmhub.configuration.models import RetryConfig
from llmhub.persistence.db import Busy, assert_not_in_tx, tx
from llmhub.provider_adapters.base import (
    Balance,
    BillingRecord,
    Capability,
    DestroyResult,
    InstanceSpec,
    NormalizedError,
    Offer,
    OfferQuery,
    ProviderAdapter,
    ProviderError,
    RemoteResource,
    UnavailableReason,
)


class CircuitBreaker:
    """closed -> open (N errors in a window) -> half_open (one probe) -> closed/open.

    State lives in `provider_accounts` so a restart keeps it. Writes are best effort: when the
    database is busy the in-memory view still decides, and the next call persists it.
    """

    def __init__(self, conn: sqlite3.Connection, provider: str, clock: Clock, cfg: RetryConfig):
        self.conn = conn
        self.provider = provider
        self.clock = clock
        self.cfg = cfg
        self._probe_inflight = False
        with tx(conn):
            conn.execute(
                "INSERT OR IGNORE INTO provider_accounts(provider) VALUES (?)", (provider,)
            )

    def _row(self) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT circuit_state, circuit_opened_at, error_ts_json FROM provider_accounts"
            " WHERE provider=?",
            (self.provider,),
        ).fetchone()
        assert isinstance(row, sqlite3.Row)
        return row

    @property
    def state(self) -> str:
        return str(self._row()["circuit_state"])

    def retry_in(self) -> float:
        row = self._row()
        opened = row["circuit_opened_at"]
        if row["circuit_state"] != "open" or opened is None:
            return 0.0
        return max(0.0, float(opened) + self.cfg.breaker_open_s - self.clock.now())

    def _write(self, state: str, opened_at: float | None, errors: list[float]) -> None:
        try:
            with tx(self.conn):
                self.conn.execute(
                    "UPDATE provider_accounts SET circuit_state=?, circuit_opened_at=?,"
                    " error_ts_json=? WHERE provider=?",
                    (state, opened_at, json.dumps(errors), self.provider),
                )
        except Busy:
            pass

    def allow(self) -> bool:
        """May a create/list_offers call go through right now?"""
        row = self._row()
        state = row["circuit_state"]
        if state == "closed":
            return True
        now = self.clock.now()
        if state == "open":
            opened = float(row["circuit_opened_at"] or 0.0)
            if now < opened + self.cfg.breaker_open_s:
                return False
            self._write("half_open", opened, json.loads(row["error_ts_json"]))
            self._probe_inflight = False
        if self._probe_inflight:
            return False
        self._probe_inflight = True
        return True

    def record_success(self) -> None:
        self._probe_inflight = False
        row = self._row()
        if row["circuit_state"] != "closed":
            self._write("closed", None, [])

    def record_error(self) -> None:
        now = self.clock.now()
        row = self._row()
        self._probe_inflight = False
        if row["circuit_state"] == "half_open":
            self._write("open", now, [now])
            return
        errors = [
            t for t in json.loads(row["error_ts_json"]) if now - t < self.cfg.breaker_window_s
        ]
        errors.append(now)
        if row["circuit_state"] == "closed" and len(errors) >= self.cfg.breaker_errors:
            self._write("open", now, errors)
        else:
            self._write(str(row["circuit_state"]), row["circuit_opened_at"], errors)


class GuardedAdapter:
    def __init__(
        self,
        inner: ProviderAdapter,
        clock: Clock,
        retry: RetryConfig,
        breaker: CircuitBreaker,
        actor: Literal["controller", "reaper", "admin"] = "controller",
    ) -> None:
        self.inner = inner
        self.clock = clock
        self.retry = retry
        self.breaker = breaker
        self.actor = actor
        self.name = inner.name
        self.capabilities: Capability = inner.capabilities

    # --- machinery -------------------------------------------------------
    async def _with_timeout[T](self, make: Callable[[], Awaitable[T]]) -> T:
        task: asyncio.Future[T] = asyncio.ensure_future(make())
        timer = asyncio.ensure_future(
            self.clock.sleep_until(self.clock.now() + self.retry.call_timeout_s)
        )
        try:
            await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            task.cancel()
            timer.cancel()
            raise
        if task.done():
            timer.cancel()
            return task.result()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task  # the call was abandoned anyway
        raise TimeoutError

    def _normalize(self, exc: BaseException, *, unknown_on_timeout: bool) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, TimeoutError):
            return ProviderError(
                NormalizedError(
                    reason=UnavailableReason.PROVIDER_API_ERROR,
                    retryable=True,
                    outcome_unknown=unknown_on_timeout,
                    retry_after_s=None,
                    http_status=None,
                    message="call timeout",
                )
            )
        return ProviderError(self.inner.normalize_error(exc))

    def _count(self, err: NormalizedError) -> None:
        if self.actor == "reaper":
            return
        if err.reason == UnavailableReason.PROVIDER_API_ERROR:
            self.breaker.record_error()

    async def _once[T](
        self, make: Callable[[], Awaitable[T]], *, unknown_on_timeout: bool = False
    ) -> T:
        assert_not_in_tx()
        try:
            return await self._with_timeout(make)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise self._normalize(exc, unknown_on_timeout=unknown_on_timeout) from exc

    async def _retried[T](self, make: Callable[[], Awaitable[T]]) -> T:
        attempt = 0
        while True:
            try:
                return await self._once(make)
            except ProviderError as err:
                self._count(err.err)
                if not err.err.retryable or attempt >= self.retry.get_retries:
                    raise
                delay = self.retry.get_backoff_s * 2**attempt
                if err.err.retry_after_s is not None:
                    delay = max(delay, err.err.retry_after_s)
                attempt += 1
                await self.clock.sleep_until(self.clock.now() + delay)

    def _gate(self) -> None:
        if self.actor != "reaper" and not self.breaker.allow():
            raise ProviderError(
                NormalizedError(
                    reason=UnavailableReason.PROVIDER_API_ERROR,
                    retryable=True,
                    outcome_unknown=False,
                    retry_after_s=self.breaker.retry_in(),
                    http_status=None,
                    message="circuit open",
                )
            )

    async def _gated[T](self, make: Callable[[], Awaitable[T]], *, create: bool) -> T:
        self._gate()
        try:
            result = await self._once(make, unknown_on_timeout=create)
        except ProviderError as err:
            self._count(err.err)
            if self.actor != "reaper" and err.err.reason != UnavailableReason.PROVIDER_API_ERROR:
                self.breaker.record_success()  # a clean business answer, not a provider fault
            raise
        if self.actor != "reaper":
            self.breaker.record_success()
        return result

    # --- ProviderAdapter -------------------------------------------------
    async def list_offers(self, q: OfferQuery) -> list[Offer]:
        return await self._gated(lambda: self.inner.list_offers(q), create=False)

    async def create(self, spec: InstanceSpec, label: str) -> RemoteResource:
        return await self._gated(lambda: self.inner.create(spec, label), create=True)

    async def get(self, ref: str) -> RemoteResource | None:
        return await self._retried(lambda: self.inner.get(ref))

    async def list_owned(self, prefix: str = "llmhub-") -> list[RemoteResource]:
        return await self._retried(lambda: self.inner.list_owned(prefix))

    async def destroy(self, ref: str) -> DestroyResult:
        return await self._once(lambda: self.inner.destroy(ref))

    async def balance(self) -> Balance | None:
        return await self._retried(lambda: self.inner.balance())

    async def billing_since(self, ts: float) -> list[BillingRecord]:
        return await self._retried(lambda: self.inner.billing_since(ts))

    def normalize_error(self, exc: BaseException) -> NormalizedError:
        return self.inner.normalize_error(exc)
