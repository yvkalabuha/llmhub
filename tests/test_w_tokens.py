"""D1: weights token issue/check (plan 2a: reusable until READY/timeout, check order)."""

import hashlib
import hmac

import pytest

from llmhub.configuration.models import TimeoutsConfig
from llmhub.weights.tokens import Verdict, check_request, issue_token, token_expiry

SECRET = b"s" * 32
OP = "0123456789ab"
OP2 = "ba9876543210"
REL = "qwen/model.gguf"
ALLOWED = frozenset({REL})
NOW = 1_000.0
EXP = 2_000


def _check(
    token: str | None,
    uri: str = f"/w/{OP}/{REL}",
    state: str | None = "LOADING_MODEL",
    now: float = NOW,
    secret: bytes = SECRET,
    header: str | None = None,
    allowed: frozenset[str] = ALLOWED,
):
    if header is None and token is not None:
        header = f"Bearer {token}"
    calls: list[str] = []

    def state_of(op: str) -> str | None:
        calls.append(op)
        return state

    return check_request(secret, header, uri, now, state_of, allowed), calls


def test_deterministic_and_format() -> None:
    t = issue_token(SECRET, OP, EXP)
    assert t == issue_token(SECRET, OP, EXP)
    v, op, e, sig = t.split(".")
    assert (v, op, e) == ("v1", OP, str(EXP)) and len(sig) == 43
    mac = hmac.new(SECRET, f"llmhub-weights|v1|{OP}|{EXP}".encode(), hashlib.sha256).digest()
    import base64

    assert sig == base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


@pytest.mark.parametrize("state", ["PROVISIONING", "BOOTING", "LOADING_MODEL"])
def test_ok_while_loading_and_reusable(state: str) -> None:
    t = issue_token(SECRET, OP, EXP)
    for _ in range(3):
        (v, op), _calls = _check(t, state=state)
        assert (v, op) == (Verdict.OK, OP)


@pytest.mark.parametrize("state", ["READY", "BUSY", "IDLE", "ERROR", "STOPPING", "STOPPED"])
def test_revoked_after_loading(state: str) -> None:
    (v, _), _ = _check(issue_token(SECRET, OP, EXP), state=state)
    assert v is Verdict.REVOKED


def test_expired_boundary() -> None:
    t = issue_token(SECRET, OP, EXP)
    assert _check(t, now=EXP - 1)[0][0] is Verdict.OK
    assert _check(t, now=EXP)[0][0] is Verdict.EXPIRED


def test_foreign_op() -> None:
    (v, _), _ = _check(issue_token(SECRET, OP, EXP), uri=f"/w/{OP2}/{REL}")
    assert v is Verdict.OP_MISMATCH


def test_bad_signature_other_secret() -> None:
    (v, _), _ = _check(issue_token(b"x" * 32, OP, EXP))
    assert v is Verdict.BAD_SIG


def test_tampered_exp_is_bad_sig() -> None:
    v, op, _, sig = issue_token(SECRET, OP, EXP).split(".")
    (verdict, _), _ = _check(f"{v}.{op}.{EXP + 500}.{sig}")
    assert verdict is Verdict.BAD_SIG


@pytest.mark.parametrize("hdr", ["Bearer", "Basic abc", "Bearer a b", "Bearer v1.x.y.z", "token"])
def test_malformed(hdr: str) -> None:
    assert _check(None, header=hdr)[0][0] is Verdict.MALFORMED


@pytest.mark.parametrize("hdr", [None, "", "   "])
def test_missing(hdr: str | None) -> None:
    assert _check(None, header=hdr)[0][0] is Verdict.MISSING


def test_unknown_op() -> None:
    (v, _), _ = _check(issue_token(SECRET, OP, EXP), state=None)
    assert v is Verdict.UNKNOWN_OP


@pytest.mark.parametrize(
    "uri",
    [
        f"/w/{OP}/other/x.gguf",
        f"/w/{OP}/qwen/../model.gguf",
        f"/w/{OP}/../qwen/model.gguf",
        f"/w/{OP}//qwen/model.gguf",
        f"/w/{OP}/qwen%2Fmodel.gguf",
        f"/w/{OP}/qwen/model.gguf?x=1",
        f"/w/{OP}/qwen/./model.gguf",
        "/other",
        f"/w/{OP}/",
    ],
)
def test_path_not_allowed(uri: str) -> None:
    (v, _), _ = _check(issue_token(SECRET, OP, EXP), uri=uri)
    assert v is Verdict.PATH


def test_d1b_state_not_consulted_before_cheap_checks() -> None:
    """Spy: DB lookup happens only after format, HMAC, exp, op and path checks passed."""
    good = issue_token(SECRET, OP, EXP)
    cases = [
        _check(None, header="garbage"),
        _check(None, header=None),
        _check(issue_token(b"x" * 32, OP, EXP)),
        _check(good, now=EXP + 1),
        _check(good, uri=f"/w/{OP2}/{REL}"),
        _check(good, uri=f"/w/{OP}/nope/x.gguf"),
    ]
    for _res, calls in cases:
        assert calls == []
    _res, calls = _check(good)
    assert calls == [OP]


def test_compare_digest_used(monkeypatch: pytest.MonkeyPatch) -> None:
    import llmhub.weights.tokens as m

    seen: list[int] = []
    real = hmac.compare_digest

    def spy(a, b):  # type: ignore[no-untyped-def]
        seen.append(1)
        return real(a, b)

    monkeypatch.setattr(m.hmac, "compare_digest", spy)
    _check(issue_token(SECRET, OP, EXP))
    assert seen


def test_token_expiry_sum_of_three() -> None:
    t = TimeoutsConfig()
    assert token_expiry(100.0, t) == 100 + 600 + 600 + 1200
    t2 = TimeoutsConfig(provisioning_s=1.5, booting_s=2, loading_model_s=3)
    assert token_expiry(10.0, t2) == 17
