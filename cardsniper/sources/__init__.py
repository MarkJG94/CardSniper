"""Listing sources. Each yields RawListing objects for the scanner to evaluate."""

from __future__ import annotations

from ..config import Config
from .base import Source
from .cardmarket import CardmarketSource
from .ebay import EbaySource
from .stores import make_store_source


def build_sources(cfg: Config) -> list[Source]:
    sources: list[Source] = []
    if cfg.cardmarket.enabled:
        sources.append(CardmarketSource(cfg))
    if cfg.ebay.enabled:
        sources.append(EbaySource(cfg))
    for store in cfg.stores:
        if store.enabled:
            sources.append(make_store_source(cfg, store))
    return sources


__all__ = ["Source", "build_sources"]
