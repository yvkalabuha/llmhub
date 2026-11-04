"""Coordinator fix: HTTP 408/429 from a source are transient, never a config error that holds the pool."""

from __future__ import annotations

import httpx
from llmhub_infer.fetch import Fetcher

from llmhub.cloud_manager.provisioner import is_config_failure


def test_429_and_408_map_to_rate_limited() -> None:
    for code in (408, 429):
        fail = Fetcher._status_fail(httpx.Response(code))  # pyright: ignore[reportPrivateUsage]
        assert fail is not None
        assert fail.reason == "rate_limited"


def test_rate_limited_is_not_a_config_failure() -> None:
    assert not is_config_failure("exhausted", ("rate_limited",))
    assert not is_config_failure("exhausted", ("http_4xx", "rate_limited"))
    assert is_config_failure("exhausted", ("http_4xx", "sha_mismatch"))
