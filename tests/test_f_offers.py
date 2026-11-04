"""F1: offer selection and pool resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from harness import default_config
from llmhub.cloud_manager.offers import select_offer
from llmhub.cloud_manager.pools import resolve_pool
from llmhub.provider_adapters.base import Offer, OfferQuery


def offer(i: int, price: float, host: str | None = None, *, vram: int = 24, ip: bool = True,
          disk: float = 0.0) -> Offer:  # fmt: skip
    return Offer(
        provider="mock", offer_id=f"o{i}", gpu="RTX4090", gpu_count=1, vram_gb=vram,
        price_usd_h=price, disk_usd_h=disk, host_id=host or f"h{i}", interruptible=False,
        public_ip=ip,
    )  # fmt: skip


Q = OfferQuery(gpu="RTX4090", min_vram_gb=24, max_price_usd_h=0.5, exclude_hosts=frozenset())


def test_cheapest_wins_including_disk() -> None:
    offers = [offer(1, 0.30), offer(2, 0.22, disk=0.10), offer(3, 0.25)]
    chosen = select_offer(offers, Q, ())
    assert chosen is not None and chosen.offer_id == "o3"  # 0.22 + 0.10 disk > 0.25


def test_blacklist_and_excluded_hosts_skipped() -> None:
    offers = [offer(1, 0.20), offer(2, 0.21), offer(3, 0.22)]
    q = OfferQuery(gpu="RTX4090", min_vram_gb=24, max_price_usd_h=0.5,
                   exclude_hosts=frozenset({"h2"}))  # fmt: skip
    chosen = select_offer(offers, q, {"h1"})
    assert chosen is not None and chosen.offer_id == "o3"


def test_max_price_vram_and_public_ip() -> None:
    offers = [offer(1, 0.60), offer(2, 0.20, vram=16), offer(3, 0.30, ip=False)]
    assert select_offer(offers, Q, ()) is not None  # o3: public IP not required
    q_ip = OfferQuery(gpu="RTX4090", min_vram_gb=24, max_price_usd_h=0.5,
                      exclude_hosts=frozenset(), need_public_ip=True)  # fmt: skip
    assert select_offer(offers, q_ip, ()) is None


def test_empty_is_none() -> None:
    assert select_offer([], Q, ()) is None


def test_resolve_pool(tmp_path: Path) -> None:
    cfg = default_config(tmp_path)
    assert resolve_pool("m1", cfg) == "p1"
    with pytest.raises(KeyError):
        resolve_pool("nope", cfg)
