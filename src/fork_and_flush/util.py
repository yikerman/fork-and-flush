"""Small helpers shared across modules."""

from __future__ import annotations


def merge(base: dict, over: dict) -> dict:
    """base with over's values, recursively for nested sections."""
    nested = {k: merge(base[k], v) for k, v in over.items() if isinstance(base.get(k), dict)}
    return {**base, **over, **nested}
