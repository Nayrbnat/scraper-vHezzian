"""Scraper self-registration registry (SPEC.md §3.16, Invariant #16).

Adding a scraper is one new file + ``@register_scraper(...)`` — no edits to
this file or ``engine.py``.  This is the conflict-free extension seam.

Usage::

    from scrapeforge.core.registry import register_scraper

    @register_scraper('ft.com', 'www.ft.com')
    class FTScraper(PremiumScraper):
        ...

``discover_scrapers()`` is called once at engine startup; it imports every
module under ``scrapeforge.scrapers`` so the decorators run automatically.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # avoid circular import; BaseScraper used as string annotation only

logger = logging.getLogger(__name__)

# Module-level registry.  Scrapers append to it via @register_scraper on import;
# agents add a NEW file and decorate it — they never edit engine.py or this dict.
_REGISTRY: dict[str, type] = {}


def register_scraper(*domains: str):
    """Class decorator that binds each *domain* to the scraper class at import time.

    Re-binding the *same* class to an already-claimed domain is idempotent (safe to
    call on repeated imports).  Binding a *different* class raises ``ValueError`` so
    two agents can never silently clobber each other's registration.

    Returns the class unchanged so the decorator is transparent.
    """

    def _wrap(cls: type) -> type:
        for domain in domains:
            existing = _REGISTRY.get(domain)
            if existing is not None and existing is not cls:
                raise ValueError(
                    f"duplicate scraper registration for '{domain}': "
                    f"already bound to {existing!r}, attempted to rebind to {cls!r}"
                )
            _REGISTRY[domain] = cls
        return cls

    return _wrap


def get_scraper_for(domain: str) -> type | None:
    """Return the scraper class for *domain*, or ``None`` if not found.

    Resolution order:
    1. Exact match (``domain`` as a key in ``_REGISTRY``).
    2. Suffix match — strip leading components one at a time until a registered
       key is a suffix of *domain* at a label boundary.  For example,
       ``www.ft.com`` resolves a registration for ``ft.com``.
    3. Return ``None`` so the engine can fall back to ``PublicScraper``.
    """
    # 1. Exact match.
    if domain in _REGISTRY:
        return _REGISTRY[domain]

    # 2. Suffix match — walk from the second label onward.
    parts = domain.split(".")
    for i in range(1, len(parts)):
        candidate = ".".join(parts[i:])
        if candidate in _REGISTRY:
            return _REGISTRY[candidate]

    return None


# Track whether discovery has already run so repeated calls are cheap.
_discovered: bool = False


def discover_scrapers() -> None:
    """Import every module under ``scrapeforge.scrapers`` so ``@register_scraper`` runs.

    Idempotent — safe to call repeatedly (subsequent calls are a fast no-op).
    Modules whose ``__name__`` starts with ``_`` are skipped.
    """
    global _discovered
    if _discovered:
        return

    import scrapeforge.scrapers as _scrapers_pkg

    prefix = _scrapers_pkg.__name__ + "."
    path = _scrapers_pkg.__path__

    for _finder, module_name, _is_pkg in pkgutil.walk_packages(path, prefix=prefix):
        # Skip private modules (starting with underscore after the last dot).
        short_name = module_name.rsplit(".", 1)[-1]
        if short_name.startswith("_"):
            continue
        try:
            importlib.import_module(module_name)
        except ImportError:
            logger.exception("discover_scrapers: skipping %r (missing optional dep)", module_name)

    _discovered = True
