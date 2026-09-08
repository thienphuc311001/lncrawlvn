"""Crawler tiers: the mini app has one tier, bundled Python crawlers."""

LEGACY = "legacy"

TIERS = (LEGACY,)


def describe(tier=None, path=None) -> str:
    return str(tier or LEGACY)
