"""Offer selection: cheapest suitable offer."""

from __future__ import annotations

from collections.abc import Collection

from llmhub.provider_adapters.base import Offer, OfferQuery


def select_offer(
    offers: list[Offer], query: OfferQuery, blacklist: Collection[str]
) -> Offer | None:
    ok = [
        o
        for o in offers
        if o.host_id not in blacklist
        and o.host_id not in query.exclude_hosts
        and o.price_usd_h <= query.max_price_usd_h
        and o.vram_gb >= query.min_vram_gb
        and (o.public_ip or not query.need_public_ip)
    ]
    return min(ok, key=lambda o: (o.price_usd_h + o.disk_usd_h, o.offer_id), default=None)
