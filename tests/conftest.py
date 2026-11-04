"""Session-wide network guard (T-371 SEC-D4).

No test may connect to the real server: the VPN net 10.8.0.0/24 (the llama-server units
and the future API on 10.8.0.1) and the public address 65.109.95.35. The guard
wraps `socket.socket.connect`/`connect_ex` (used by httpx, asyncio and http.client alike) and
raises `PermissionError` before a packet leaves. Subprocesses and containers are not covered:
the deploy test (F5) runs its "server" with `--network none` for that reason.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

BLOCKED_NETS = (ipaddress.ip_network("10.8.0.0/24"), ipaddress.ip_network("65.109.95.35/32"))


def blocked_address(address: Any) -> bool:
    """True for an AF_INET/AF_INET6 address tuple whose host is (or maps to) a blocked IP."""
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode(errors="replace")
    if not isinstance(host, str):
        return False
    try:
        ip = ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False  # a name: resolution happens first and connects to an IP again
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in BLOCKED_NETS)


BLOCKED_ATTEMPTS: list[str] = []  # every refused connect (tests check the guard was hit)

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _refuse(address: Any) -> PermissionError:
    BLOCKED_ATTEMPTS.append(repr(address))
    return PermissionError(f"test network guard: connect to {address!r} is forbidden")


def _guarded_connect(self: socket.socket, address: Any) -> None:
    if blocked_address(address):
        raise _refuse(address)
    _real_connect(self, address)


def _guarded_connect_ex(self: socket.socket, address: Any) -> int:
    if blocked_address(address):
        raise _refuse(address)
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
