"""Provider-independent adapter contract C1..C10 (plan section 7.2).

Every real adapter (T-372) must pass `run_contract` against its own harness (sandbox or
recorded fixtures); MockProvider passes it in `tests/test_b_contract.py`.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Awaitable
from typing import Protocol

from llmhub.provider_adapters.base import (
    Capability,
    DestroyResult,
    InstanceSpec,
    NormalizedError,
    ProviderAdapter,
    ProviderError,
    RemoteStatus,
    UnavailableReason,
)
from llmhub.provider_adapters.labels import make_label, new_op_id

CONTRACT_ITEMS: tuple[str, ...] = tuple(f"C{i}" for i in range(1, 11))
FORBIDDEN_METHODS = ("stop", "pause", "start", "resume")
_SECRET_ERROR_TEXT = "boom api_key=sk-SECRET-VALUE-123"


class AdapterHarness(Protocol):
    """How the contract drives time and visibility for a given provider."""

    @property
    def adapter(self) -> ProviderAdapter: ...

    @property
    def lagging(self) -> bool:
        """Destroy/visibility lag is configured: removal is not instant."""
        ...

    async def settle(self) -> None:
        """Let the provider converge: boot finished, lists caught up, destroys completed."""
        ...

    def make_spec(self) -> InstanceSpec: ...

    def add_foreign(self, label: str) -> str:
        """Create a resource that does not belong to llmhub (other label / bad HMAC)."""
        ...

    def inject_unknown_create(self) -> None:
        """The next `create` ends with an unknown outcome (timeout)."""
        ...


def _check(ok: bool, item: str, msg: str) -> None:
    if not ok:
        raise AssertionError(f"{item}: {msg}")


async def _c1(h: AdapterHarness) -> None:
    for name in FORBIDDEN_METHODS:
        _check(not hasattr(h.adapter, name), "C1", f"adapter has forbidden attribute {name!r}")
        _check(not hasattr(type(h.adapter), name), "C1", f"class has forbidden {name!r}")


async def _c2(h: AdapterHarness, secret: bytes) -> str:
    label = make_label(new_op_id(), secret)
    res = await h.adapter.create(h.make_spec(), label)
    _check(res.label == label, "C2", "RemoteResource.label differs from the requested label")
    _check(bool(res.ref), "C2", "empty ref")
    return res.ref


async def _c3(h: AdapterHarness, secret: bytes, ref: str) -> None:
    foreign = "someone-elses-instance"
    h.add_foreign(foreign)
    await h.settle()
    owned = await h.adapter.list_owned("llmhub-")
    _check(any(r.ref == ref for r in owned), "C3", "created resource not in list_owned")
    _check(
        all(r.label.startswith("llmhub-") for r in owned), "C3", "list_owned returned a foreign one"
    )


async def _c4(h: AdapterHarness, ref: str) -> None:
    res = await h.adapter.get(ref)
    _check(
        res is not None and res.status == RemoteStatus.RUNNING,
        "C4",
        "get(ref) is not RUNNING after settle",
    )
    _check(await h.adapter.get("no-such-ref-0000") is None, "C4", "get(unknown) is not None")


async def _c5(h: AdapterHarness, ref: str) -> None:
    first = await h.adapter.destroy(ref)
    _check(first == DestroyResult.ACCEPTED, "C5", f"first destroy returned {first}")
    second = await h.adapter.destroy(ref)
    _check(
        second in (DestroyResult.ACCEPTED, DestroyResult.ALREADY_GONE),
        "C5",
        f"second destroy returned {second}",
    )
    unknown = await h.adapter.destroy("no-such-ref-0000")
    _check(unknown == DestroyResult.ALREADY_GONE, "C5", "destroy(unknown) is not ALREADY_GONE")


async def _c6(h: AdapterHarness, ref: str) -> None:
    if h.lagging:
        # destroy() returning must not be taken as proof of removal; only the list is.
        owned = await h.adapter.list_owned("llmhub-")
        _check(
            any(r.ref == ref for r in owned),
            "C6",
            "lagging adapter already hides the resource right after destroy",
        )
    await h.settle()
    owned = await h.adapter.list_owned("llmhub-")
    _check(all(r.ref != ref for r in owned), "C6", "resource still listed after destroy+settle")


async def _c7(h: AdapterHarness, secret: bytes) -> None:
    excs: list[BaseException] = [
        ValueError(_SECRET_ERROR_TEXT),
        TimeoutError(_SECRET_ERROR_TEXT),
        RuntimeError(_SECRET_ERROR_TEXT),
        KeyError(_SECRET_ERROR_TEXT),
    ]
    for exc in excs:
        err = h.adapter.normalize_error(exc)
        _check(isinstance(err, NormalizedError), "C7", "not a NormalizedError")
        _check(isinstance(err.reason, UnavailableReason), "C7", "reason outside the enum")
        _check("sk-SECRET" not in err.message, "C7", "secret leaked into message")
        _check(secret.decode(errors="ignore") not in err.message, "C7", "label secret in message")


async def _c8(h: AdapterHarness) -> None:
    caps = h.adapter.capabilities
    bal = await h.adapter.balance()
    _check(
        (Capability.BALANCE_API in caps) == (bal is not None),
        "C8",
        "BALANCE_API flag does not match balance()",
    )
    res = await h.adapter.create(h.make_spec(), make_label(new_op_id(), b"x" * 32))
    try:
        await h.settle()
        records = await h.adapter.billing_since(0.0)
        _check(
            (Capability.BILLING_API in caps) == bool(records),
            "C8",
            "BILLING_API flag does not match billing_since()",
        )
    finally:
        await h.adapter.destroy(res.ref)
        await h.settle()


async def _c9(h: AdapterHarness, secret: bytes) -> None:
    h.inject_unknown_create()
    label = make_label(new_op_id(), secret)
    try:
        res = await h.adapter.create(h.make_spec(), label)
    except ProviderError as exc:
        _check(exc.err.outcome_unknown, "C9", "timeout during create is not outcome_unknown")
    else:
        await h.adapter.destroy(res.ref)
        raise AssertionError("C9: create with an unknown outcome did not raise")
    await h.settle()
    # No hidden retry: at most one resource with this label may exist.
    owned = [r for r in await h.adapter.list_owned("llmhub-") if r.label == label]
    _check(len(owned) <= 1, "C9", "adapter retried create behind the caller's back")
    for r in owned:
        await h.adapter.destroy(r.ref)
    await h.settle()


async def _c10(h: AdapterHarness) -> None:
    module = type(h.adapter).__module__
    code = (
        "import socket\n"
        "def _deny(*a, **k):\n"
        "    raise RuntimeError('network call at import')\n"
        "socket.socket.connect = _deny\n"
        "socket.create_connection = _deny\n"
        "socket.getaddrinfo = _deny\n"
        f"import {module}\n"
    )
    proc = await asyncio.to_thread(
        subprocess.run, [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    _check(proc.returncode == 0, "C10", f"import of {module} touched the network: {proc.stderr}")


async def _item[T](item: str, coro: Awaitable[T]) -> T:
    try:
        return await coro
    except AssertionError:
        raise
    except Exception as exc:
        raise AssertionError(f"{item}: unexpected {type(exc).__name__}: {exc}") from exc


async def run_contract(h: AdapterHarness, secret: bytes) -> None:
    """Run C1..C10; raises AssertionError whose message starts with the failed item id."""
    await _item("C1", _c1(h))
    ref = await _item("C2", _c2(h, secret))
    try:
        await h.settle()
        await _item("C3", _c3(h, secret, ref))
        await _item("C4", _c4(h, ref))
    except BaseException:
        await h.adapter.destroy(ref)
        raise
    await _item("C5", _c5(h, ref))
    await _item("C6", _c6(h, ref))
    await _item("C7", _c7(h, secret))
    await _item("C8", _c8(h))
    await _item("C9", _c9(h, secret))
    await _item("C10", _c10(h))
