"""R2: decide() over the rule table H1-H5, P1-P5, F1-F3 (plan 3.5), one test per row."""

from __future__ import annotations

import itertools
from dataclasses import replace

import pytest

from llmhub.configuration.api_models import RouterConfig
from llmhub.model_router.rules import (
    CloudView,
    LocalView,
    RouteDecision,
    RouteError,
    RouteErrorCode,
    RouteRequest,
    decide,
)
from llmhub.provider_adapters.base import UnavailableReason as UR
from llmhub.task_queue.models import Priority

CFG = RouterConfig(economy_local_max_eta_s=3600, balanced_local_max_eta_s=600)


def rq(**kw: object) -> RouteRequest:
    base = {
        "alias": "m1", "task_type": None, "priority": Priority.NORMAL, "policy": "BALANCED",
        "deadline_in_s": None, "data_class": "cloud_allowed", "secret_hits": (),
        "tokens_in": 1000, "max_tokens": 500,
    }  # fmt: skip
    return replace(RouteRequest(**base), **kw)  # type: ignore[arg-type]


def lv(**kw: object) -> LocalView:
    base = {"backend": "q8", "ctx_tokens": 32768, "eta_s": 100.0, "available": True}
    return LocalView(**{**base, **kw})  # type: ignore[arg-type]


def cv(**kw: object) -> CloudView:
    base = {"pool": "p1", "ctx_tokens": 65536, "warm": False, "eta_s": 700.0,
            "unavailable": None, "est_cost_usd": 0.05}  # fmt: skip
    return CloudView(**{**base, **kw})  # type: ignore[arg-type]


def err(req: RouteRequest, local: LocalView, cloud: CloudView) -> RouteError:
    with pytest.raises(RouteError) as ei:
        decide(req, local, cloud, CFG)
    return ei.value


def ok(req: RouteRequest, local: LocalView, cloud: CloudView) -> RouteDecision:
    return decide(req, local, cloud, CFG)


# ---- H1 private ----------------------------------------------------------------------------
def test_h1_private_goes_local_even_when_cloud_is_warm() -> None:
    d = ok(rq(data_class="private", policy="QUALITY"), lv(), cv(warm=True))
    assert (d.route, d.backend, d.pool, d.reason, d.est_cost_usd) == (
        "local",
        "q8",
        None,
        "private",
        None,
    )


def test_h1_private_without_local_is_forbidden() -> None:
    e = err(
        rq(data_class="private"),
        lv(backend=None, ctx_tokens=None, eta_s=None, available=False),
        cv(),
    )
    assert (e.code, e.reason) == (RouteErrorCode.FORBIDDEN, "private_requires_local")


def test_h1_private_context_over_local_is_400_not_cloud() -> None:
    e = err(rq(data_class="private", tokens_in=40000), lv(), cv(warm=True))
    assert e.code == RouteErrorCode.CONTEXT_TOO_LARGE


# ---- H2 secrets ----------------------------------------------------------------------------
def test_h2_secret_goes_local_with_warning() -> None:
    d = ok(rq(secret_hits=("jwt",)), lv(), cv(warm=True))
    assert (d.route, d.reason, d.warnings) == ("local", "secret_detected", ("secret_detected",))


def test_h2_secret_without_local_is_forbidden() -> None:
    e = err(
        rq(secret_hits=("jwt",)),
        lv(backend=None, ctx_tokens=None, eta_s=None, available=False),
        cv(),
    )
    assert (e.code, e.reason) == (RouteErrorCode.FORBIDDEN, "secret_detected")


def test_h2_secret_context_over_local_is_400() -> None:
    e = err(rq(secret_hits=("jwt",), max_tokens=40000), lv(), cv())
    assert e.code == RouteErrorCode.CONTEXT_TOO_LARGE


def test_h1_private_with_secret_keeps_both_marks() -> None:
    d = ok(rq(data_class="private", secret_hits=("jwt",)), lv(), cv())
    assert (d.reason, d.warnings) == ("private", ("secret_detected",))


# ---- H3 / H4 / H5 --------------------------------------------------------------------------
def test_h3_big_context_goes_cloud_only() -> None:
    d = ok(rq(policy="ECONOMY", tokens_in=40000), lv(), cv())
    assert (d.route, d.pool, d.reason) == ("cloud", "p1", "ctx_local_too_small")


def test_h3_big_context_cloud_unavailable_is_503_with_reason() -> None:
    e = err(rq(tokens_in=40000), lv(), cv(unavailable=UR.INSUFFICIENT_BALANCE))
    assert (e.code, e.reason) == (RouteErrorCode.CLOUD_UNAVAILABLE, "insufficient_balance")


def test_h3_breaker_retry_after_is_copied() -> None:
    e = err(rq(tokens_in=40000), lv(), cv(unavailable=UR.PROVIDER_API_ERROR, retry_after_s=420.0))
    assert e.retry_after_s == 420.0


def test_h4_context_over_both_is_400() -> None:
    e = err(rq(tokens_in=70000), lv(), cv())
    assert e.code == RouteErrorCode.CONTEXT_TOO_LARGE


def test_h4_context_over_local_without_cloud_is_400() -> None:
    e = err(rq(tokens_in=40000), lv(), cv(pool=None, ctx_tokens=0))
    assert e.code == RouteErrorCode.CONTEXT_TOO_LARGE


def test_h5_no_model_anywhere_is_404() -> None:
    e = err(
        rq(),
        lv(backend=None, ctx_tokens=None, eta_s=None, available=False),
        cv(pool=None, ctx_tokens=0),
    )
    assert e.code == RouteErrorCode.MODEL_NOT_AVAILABLE


def test_cloud_only_alias() -> None:
    d = ok(rq(), lv(backend=None, ctx_tokens=None, eta_s=None, available=False), cv())
    assert (d.route, d.reason) == ("cloud", "cloud_only")


def test_local_only_alias_and_local_down_is_503_local() -> None:
    nocloud = cv(pool=None, ctx_tokens=0)
    assert ok(rq(), lv(), nocloud).route == "local"
    e = err(rq(), lv(available=False, eta_s=None), nocloud)
    assert e.code == RouteErrorCode.LOCAL_UNAVAILABLE


# ---- P1 ECONOMY ----------------------------------------------------------------------------
def test_p1_economy_local_within_eta() -> None:
    d = ok(rq(policy="ECONOMY"), lv(eta_s=3600.0), cv(warm=True))
    assert (d.route, d.reason) == ("local", "economy_local")


def test_p1_economy_local_too_slow_goes_cloud() -> None:
    d = ok(rq(policy="ECONOMY"), lv(eta_s=3601.0), cv())
    assert (d.route, d.reason, d.fallback) == ("cloud", "economy_cloud", None)


def test_p1_economy_deadline_tighter_than_cap() -> None:
    d = ok(rq(policy="ECONOMY", deadline_in_s=200.0), lv(eta_s=300.0), cv())
    assert d.route == "cloud"
    assert ok(rq(policy="ECONOMY", deadline_in_s=200.0), lv(eta_s=200.0), cv()).route == "local"


# ---- P2 / P3 BALANCED ----------------------------------------------------------------------
def test_p2_balanced_warm_cloud_wins() -> None:
    d = ok(rq(), lv(eta_s=1.0), cv(warm=True))
    assert (d.route, d.reason, d.est_cost_usd) == ("cloud", "balanced_warm", 0.05)


def test_p2_warm_but_unavailable_falls_to_p3() -> None:
    d = ok(rq(), lv(), cv(warm=True, unavailable="controller_halted"))
    assert (d.route, d.reason) == ("local", "balanced_local")


def test_p3_balanced_local_within_600s() -> None:
    assert ok(rq(), lv(eta_s=600.0), cv()).route == "local"


def test_p3_balanced_local_slow_goes_cold_cloud() -> None:
    d = ok(rq(), lv(eta_s=601.0), cv())
    assert (d.route, d.reason) == ("cloud", "balanced_cloud")


def test_p3_balanced_deadline_beats_cap() -> None:
    assert ok(rq(deadline_in_s=50.0), lv(eta_s=100.0), cv()).route == "cloud"


def test_doc_example_8000_tokens_cloud_1000_tokens_local() -> None:
    """Research 6.11: 8000 tokens would take 14-16 min locally -> cloud; ~2 min -> local."""
    assert ok(rq(tokens_in=7500, max_tokens=500), lv(eta_s=900.0), cv()).route == "cloud"
    assert ok(rq(tokens_in=800, max_tokens=200), lv(eta_s=120.0), cv()).route == "local"


# ---- P4 QUALITY ----------------------------------------------------------------------------
def test_p4_quality_prefers_cloud_even_cold() -> None:
    d = ok(rq(policy="QUALITY"), lv(eta_s=1.0), cv())
    assert (d.route, d.reason) == ("cloud", "quality_cloud")


def test_p4_quality_cloud_down_goes_local_with_fallback() -> None:
    d = ok(rq(policy="QUALITY"), lv(), cv(unavailable=UR.QUOTA_EXCEEDED))
    assert (d.route, d.fallback) == ("local", "quota_exceeded")


# ---- P5 / F3 budget stages (the view reports budget_limit_reached) -------------------------
def test_p5_urgent_only_stage_makes_cloud_unavailable_for_normal() -> None:
    d = ok(rq(policy="QUALITY"), lv(), cv(unavailable=UR.BUDGET_LIMIT_REACHED))
    assert (d.route, d.fallback) == ("local", "budget_limit_reached")


def test_p5_warm_cloud_unavailable_for_normal_goes_local() -> None:
    d = ok(rq(), lv(eta_s=9999.0), cv(warm=True, unavailable=UR.BUDGET_LIMIT_REACHED))
    assert (d.route, d.fallback) == ("local", "budget_limit_reached")


def test_p5_urgent_keeps_cloud() -> None:
    d = ok(rq(policy="QUALITY", priority=Priority.URGENT), lv(), cv())
    assert d.route == "cloud"


def test_f3_no_launch_closes_cloud_even_warm_urgent() -> None:
    # the view reports budget_limit_reached for every priority, warm or cold
    for warm in (False, True):
        d = ok(
            rq(priority=Priority.URGENT),
            lv(eta_s=9999.0),
            cv(warm=warm, unavailable=UR.BUDGET_LIMIT_REACHED),
        )
        assert (d.route, d.fallback) == ("local", "budget_limit_reached")
    d = ok(rq(), lv(eta_s=9999.0), cv(unavailable=UR.BUDGET_LIMIT_REACHED))
    assert (d.route, d.fallback) == ("local", "budget_limit_reached")
    e = err(rq(tokens_in=40000), lv(), cv(unavailable=UR.BUDGET_LIMIT_REACHED))
    assert (e.code, e.reason) == (RouteErrorCode.CLOUD_UNAVAILABLE, "budget_limit_reached")


# ---- F1 cloud unavailable ------------------------------------------------------------------
@pytest.mark.parametrize(
    "reason",
    [UR.INSUFFICIENT_BALANCE, UR.QUOTA_EXCEEDED, UR.PROVIDER_API_ERROR, UR.BUDGET_LIMIT_REACHED,
     UR.GPU_UNAVAILABLE, UR.DEPLOYMENT_ERROR, "controller_halted"],
)  # fmt: skip
def test_f1_cloud_unavailable_falls_back_local_with_reason(reason: object) -> None:
    d = ok(rq(policy="QUALITY"), lv(), cv(unavailable=reason))
    assert (d.route, d.fallback) == ("local", str(reason))
    d2 = ok(rq(), lv(eta_s=9999.0), cv(unavailable=reason))  # BALANCED, local slow but only option
    assert (d2.route, d2.fallback) == ("local", str(reason))


def test_f1_both_down_is_503_cloud_unavailable_for_quality() -> None:
    e = err(
        rq(policy="QUALITY"), lv(available=False, eta_s=None), cv(unavailable=UR.QUOTA_EXCEEDED)
    )
    assert (e.code, e.reason) == (RouteErrorCode.CLOUD_UNAVAILABLE, "quota_exceeded")


# ---- F2 local unavailable ------------------------------------------------------------------
@pytest.mark.parametrize("policy", ["ECONOMY", "BALANCED"])
def test_f2_local_down_goes_cloud_when_allowed(policy: str) -> None:
    d = ok(rq(policy=policy), lv(available=False, eta_s=None), cv())
    assert (d.route, d.fallback) == ("cloud", "local_unavailable")


def test_f2_both_down_is_503_local_unavailable() -> None:
    e = err(rq(), lv(available=False, eta_s=None), cv(unavailable=UR.QUOTA_EXCEEDED))
    assert e.code == RouteErrorCode.LOCAL_UNAVAILABLE


POLICIES = ["ECONOMY", "BALANCED", "QUALITY"]
PRIOS = list(Priority)
CLOUD_STATES = [
    cv(), cv(warm=True), cv(unavailable=UR.BUDGET_LIMIT_REACHED), cv(warm=True, unavailable="controller_halted"),
    cv(warm=True, unavailable=UR.BUDGET_LIMIT_REACHED),
]  # fmt: skip
LOCAL_STATES = [
    lv(), lv(available=False, eta_s=None), lv(eta_s=99999.0), lv(eta_s=None), lv(backend=None, ctx_tokens=None, eta_s=None),
]  # fmt: skip


@pytest.mark.parametrize(
    ("policy", "prio", "cloud", "local", "dc", "hits"),
    list(itertools.product(
        POLICIES, PRIOS, CLOUD_STATES, LOCAL_STATES,
        ["private", "cloud_allowed"], [(), ("sk_key",)],
    )),
)  # fmt: skip
def test_f2_negative_private_or_secret_never_reaches_cloud(
    policy: str, prio: Priority, cloud: CloudView, local: LocalView, dc: str, hits: tuple[str, ...]
) -> None:
    req = rq(policy=policy, priority=prio, data_class=dc, secret_hits=hits)
    private_like = dc == "private" or bool(hits)
    try:
        d = decide(req, local, cloud, CFG)
    except RouteError as e:
        if private_like:
            assert e.code in {RouteErrorCode.LOCAL_UNAVAILABLE, RouteErrorCode.FORBIDDEN}
        return
    if private_like:
        assert d.route == "local" and d.pool is None and d.est_cost_usd is None


def test_f2_explicit_negative_cases() -> None:
    down = lv(available=False, eta_s=None)
    warm = cv(warm=True)
    for kw in ({"data_class": "private"}, {"secret_hits": ("jwt",)}):
        e = err(rq(**kw), down, warm)
        assert e.code == RouteErrorCode.LOCAL_UNAVAILABLE


# ---- determinism ---------------------------------------------------------------------------
def test_decide_is_deterministic() -> None:
    args = (rq(), lv(eta_s=450.0), cv())
    assert decide(*args, CFG) == decide(*args, CFG)
