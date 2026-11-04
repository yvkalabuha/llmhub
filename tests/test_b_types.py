import dataclasses

import pytest

from llmhub.cloud_manager.inference import JobRunResult, JobRunSpec
from llmhub.provider_adapters import base
from llmhub.provider_adapters.contract import FORBIDDEN_METHODS


@pytest.mark.parametrize(
    "obj",
    [
        base.OfferQuery("g", 24, 0.5, frozenset()),
        base.Offer("m", "o", "g", 1, 24, 0.2, 0.0, "h", False, True),
        base.InstanceSpec("p", "o", "i", 50, 0.5, 100, {}),
        base.RemoteResource(
            "m",
            "r",
            "l",
            base.ResourceKind.INSTANCE,
            base.RemoteStatus.RUNNING,
            None,
            None,
            0.2,
            None,
            0.0,
        ),
        base.Balance(1.0, 0.0),
        base.BillingRecord(None, "instance", 0.0, 1.0, 0.1),
        base.NormalizedError(base.UnavailableReason.GPU_UNAVAILABLE, False, False, None, 409, "m"),
        JobRunSpec("j", "m", 1, 1, b""),
        JobRunResult(1, 1, "stop", b""),
    ],
)
def test_dataclasses_are_frozen(obj: object) -> None:
    field = dataclasses.fields(obj)[0].name  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(obj, field, None)


def test_protocol_has_no_stop() -> None:
    for name in FORBIDDEN_METHODS:
        assert not hasattr(base.ProviderAdapter, name)


def test_provider_error_carries_normalized_error() -> None:
    err = base.NormalizedError(base.UnavailableReason.QUOTA_EXCEEDED, False, False, None, 403, "q")
    assert base.ProviderError(err).err is err
