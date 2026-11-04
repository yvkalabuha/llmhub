"""Pure routing: alias resolution and the rule table H1-H5, P1-P5, F1-F3 (T-369 plan 3.5).

`decide` has no I/O and no clock: same input, same output. Budget stages (P5, F3) arrive as
`CloudView.unavailable == budget_limit_reached` (computed by `views.CloudState`); the ECONOMY
policy under stage >= 80 % arrives already applied in `RouteRequest.policy`.
Hard invariant (critic high, F2): a `private` request or one with `secret_hits` is NEVER routed
to the cloud, whatever the policy, the budget stage or the state of the local backend.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from llmhub.configuration.api_models import AUTO_ALIAS, Policy, RouterConfig
from llmhub.configuration.models import Config
from llmhub.provider_adapters.base import UnavailableReason
from llmhub.task_queue.models import Priority

CONTROLLER_HALTED: Literal["controller_halted"] = "controller_halted"
SECRET_WARNING = "secret_detected"


class RouteErrorCode(enum.StrEnum):
    """Subset of the API `ErrorCode` values; the gateway maps by value."""

    FORBIDDEN = "forbidden"
    CONTEXT_TOO_LARGE = "context_too_large"
    MODEL_NOT_AVAILABLE = "model_not_available"
    LOCAL_UNAVAILABLE = "local_unavailable"
    CLOUD_UNAVAILABLE = "cloud_unavailable"


class RouteError(Exception):
    def __init__(
        self,
        code: RouteErrorCode,
        *,
        reason: str | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(f"{code.value}:{reason}" if reason else code.value)
        self.code = code
        self.reason = reason
        self.retry_after_s = retry_after_s


@dataclass(frozen=True)
class RouteRequest:
    alias: str  # already expanded from "auto"
    task_type: str | None
    priority: Priority
    policy: Policy  # with the budget stage applied (ECONOMY at >= 80 % for normal/low)
    deadline_in_s: float | None
    data_class: Literal["private", "cloud_allowed"]  # effective: client AND header AND field
    secret_hits: tuple[str, ...]
    tokens_in: int
    max_tokens: int


@dataclass(frozen=True)
class LocalView:
    backend: str | None
    ctx_tokens: int | None
    eta_s: float | None
    available: bool


@dataclass(frozen=True)
class CloudView:
    pool: str | None
    ctx_tokens: int
    warm: bool
    eta_s: float  # work only when warm, otherwise t_cold_p90 + work
    unavailable: UnavailableReason | Literal["controller_halted"] | None
    est_cost_usd: float
    retry_after_s: float | None = None  # breaker / hold remainder, copied into 503


@dataclass(frozen=True)
class RouteDecision:
    route: Literal["local", "cloud"]
    pool: str | None
    backend: str | None
    reason: str
    fallback: str | None
    warnings: tuple[str, ...]
    est_cost_usd: float | None


def resolve_alias(
    requested: str,
    policy: Policy,
    cfg: Config,
    allowed: Callable[[str], bool],
    *,
    cloud_allowed: bool = False,
) -> str:
    """`auto` -> `router.auto_alias`; QUALITY + auto + cloud_allowed -> the allowed alias with the
    highest `quality_tier` that has a cloud pool. Unknown -> model_not_available, denied -> forbidden."""
    alias = requested
    if requested == AUTO_ALIAS:
        alias = cfg.router.auto_alias or ""
        if policy == "QUALITY" and cloud_allowed:
            cloud_aliases = {m for p in cfg.pools.values() for m in p.models}
            best = [
                (m.quality_tier, name)
                for name, m in cfg.models.items()
                if name in cloud_aliases and allowed(name)
            ]
            if best:
                top = max(t for t, _ in best)
                alias = next(n for t, n in best if t == top)  # config order breaks ties
    if alias not in cfg.models:
        raise RouteError(RouteErrorCode.MODEL_NOT_AVAILABLE, reason="unknown_model")
    if not allowed(alias):
        raise RouteError(RouteErrorCode.FORBIDDEN, reason="model_not_allowed")
    return alias


def _cloud_error(cloud: CloudView) -> RouteError:
    return RouteError(
        RouteErrorCode.CLOUD_UNAVAILABLE,
        reason=str(cloud.unavailable),
        retry_after_s=cloud.retry_after_s,
    )


def _local(
    req: RouteRequest, local: LocalView, reason: str, fallback: str | None = None
) -> RouteDecision:
    warn = (SECRET_WARNING,) if req.secret_hits else ()
    return RouteDecision("local", None, local.backend, reason, fallback, warn, None)


def _cloud(
    req: RouteRequest, cloud: CloudView, reason: str, fallback: str | None = None
) -> RouteDecision:
    assert not req.secret_hits and req.data_class == "cloud_allowed"  # F2 invariant
    return RouteDecision("cloud", cloud.pool, None, reason, fallback, (), cloud.est_cost_usd)


def _local_only(
    req: RouteRequest, local: LocalView, reason: str, need: int, *, no_local: str
) -> RouteDecision:
    """H1/H2: the request may not leave the machine."""
    if local.backend is None:
        raise RouteError(RouteErrorCode.FORBIDDEN, reason=no_local)
    if local.ctx_tokens is not None and need > local.ctx_tokens:
        raise RouteError(RouteErrorCode.CONTEXT_TOO_LARGE, reason="local_context")
    if not local.available:
        raise RouteError(RouteErrorCode.LOCAL_UNAVAILABLE, reason="local_unavailable")  # F2
    return _local(req, local, reason)


def decide(
    req: RouteRequest, local: LocalView, cloud: CloudView, cfg: RouterConfig
) -> RouteDecision:
    need = req.tokens_in + req.max_tokens
    has_local = local.backend is not None
    has_cloud = cloud.pool is not None
    if not has_local and not has_cloud:  # H5
        raise RouteError(RouteErrorCode.MODEL_NOT_AVAILABLE, reason="no_backend")
    if req.data_class == "private":  # H1
        return _local_only(req, local, "private", need, no_local="private_requires_local")
    if req.secret_hits:  # H2
        return _local_only(req, local, "secret_detected", need, no_local="secret_detected")

    fits_local = has_local and local.ctx_tokens is not None and need <= local.ctx_tokens
    fits_cloud = has_cloud and need <= cloud.ctx_tokens
    if not fits_local and not fits_cloud:  # H4
        raise RouteError(RouteErrorCode.CONTEXT_TOO_LARGE, reason="context")
    local_ok = fits_local and local.available and local.eta_s is not None
    cloud_ok = fits_cloud and cloud.unavailable is None

    if not fits_cloud:  # local is the only candidate
        if not local_ok:
            raise RouteError(RouteErrorCode.LOCAL_UNAVAILABLE, reason="local_unavailable")
        return _local(req, local, "local_only")
    if not fits_local:  # H3: only the cloud
        if not cloud_ok:  # F1: no local to fall back to
            raise _cloud_error(cloud)
        return _cloud(req, cloud, "ctx_local_too_small" if has_local else "cloud_only")

    # both candidates fit: policy
    d = req.deadline_in_s if req.deadline_in_s is not None else float("inf")
    eta = local.eta_s if local.eta_s is not None else float("inf")
    if req.policy == "QUALITY":  # P4
        if cloud_ok:
            return _cloud(req, cloud, "quality_cloud")
        if local_ok:  # F1
            return _local(req, local, "quality_fallback", str(cloud.unavailable))
        raise _cloud_error(cloud)
    if req.policy == "BALANCED" and cloud_ok and cloud.warm:  # P2
        return _cloud(req, cloud, "balanced_warm")
    limit = cfg.economy_local_max_eta_s if req.policy == "ECONOMY" else cfg.balanced_local_max_eta_s
    prefix = "economy" if req.policy == "ECONOMY" else "balanced"
    if local_ok and eta <= min(d, limit):  # P1 / P3
        return _local(req, local, f"{prefix}_local")
    if not local.available or local.eta_s is None:  # local wanted but down
        if cloud_ok:  # F2 (cloud_allowed and no secrets are guaranteed above)
            return _cloud(req, cloud, f"{prefix}_cloud", "local_unavailable")
        raise RouteError(RouteErrorCode.LOCAL_UNAVAILABLE, reason="local_unavailable")
    if cloud_ok:
        return _cloud(req, cloud, f"{prefix}_cloud")
    return _local(req, local, f"{prefix}_fallback", str(cloud.unavailable))  # F1, local too slow
