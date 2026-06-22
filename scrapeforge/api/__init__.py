"""ScrapeForge serving API — read + enqueue plane (SPEC.md §3.22, Invariant #18).

This package is a FastAPI application that:
- Serves article data from Postgres (read-only queries via repositories).
- Accepts job submissions and publishes them to the queue (enqueue-only; never drives a browser).
- Enforces X-API-Key authentication with per-key rate limiting.

Entry point for uvicorn::

    uvicorn scrapeforge.api.app:create_app --factory --host 0.0.0.0 --port 8000
"""
