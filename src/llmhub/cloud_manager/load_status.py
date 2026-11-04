"""Optional weights/model loading progress of a pod (T-370 stream E).

A SEPARATE protocol next to the frozen `InferenceBackend`: the provisioner checks it with
`isinstance(backend, LoadStatusSource)` and falls back to the old health/test behaviour
when the backend does not implement it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, cast, runtime_checkable

LoadStatusKind = Literal["loading", "ok", "failed"]
_STATUSES: frozenset[str] = frozenset({"loading", "ok", "failed"})


@dataclass(frozen=True)
class LoadStatus:
    status: LoadStatusKind
    phase: str | None = None
    source: str | None = None
    done: int = 0
    total: int = 0
    mb_s: float = 0.0
    reason: str | None = None
    eta_s: float | None = None  # None = unknown
    sources_left: int | None = None  # None = unknown

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@runtime_checkable
class LoadStatusSource(Protocol):
    async def load_status(self, endpoint: str) -> LoadStatus | None: ...


def _num(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, int | float):
        return None
    f = float(v)
    return f if math.isfinite(f) and f >= 0 else None  # NaN, inf, negative -> unknown


def _str(v: object, limit: int = 64) -> str | None:
    return v[:limit] if isinstance(v, str) else None


def parse_load_status(body: object) -> LoadStatus | None:
    """`/health` JSON of `llmhub-infer` -> LoadStatus. Anything that is not ours -> None.

    A RunPod proxy page (HTML, or JSON without our `status`) is `None`, never `failed`.
    """
    if not isinstance(body, Mapping):
        return None
    status = body.get("status")
    if not isinstance(status, str) or status not in _STATUSES:
        return None
    done = _num(body.get("done"))
    total = _num(body.get("total"))
    left = _num(body.get("sources_left"))
    return LoadStatus(
        status=cast(LoadStatusKind, status),  # checked against _STATUSES
        phase=_str(body.get("phase")),
        source=_str(body.get("source")),
        done=int(done or 0),
        total=int(total or 0),
        mb_s=_num(body.get("mb_s")) or 0.0,
        reason=_str(body.get("reason")),
        eta_s=_num(body.get("eta_s")),
        sources_left=None if left is None else int(left),
    )
