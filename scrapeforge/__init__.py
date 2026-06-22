"""ScrapeForge — multi-bucket anti-detection scraper (ingestion + serving planes).

The package root. Subpackages:
- ``core``      — infrastructure: engine, registry, drivers, bridge, proxy, fingerprint,
                  rate limiter, circuit breaker, storage sinks, data models.
- ``scrapers``  — the three buckets (premium / community / public); one file per site.
- ``utils``     — humanization, DOM parsers, response validators.
- ``config``    — env-driven settings.

See ``SPEC.md`` for class contracts and ``architecture.MD`` for the module map.
"""

__version__ = "0.1.0"
