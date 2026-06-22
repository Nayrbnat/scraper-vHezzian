"""API route sub-package for ScrapeForge (SPEC.md §3.22).

Routers registered here:
- ``health``   — GET /health, GET /ready (no auth)
- ``articles`` — GET /articles, GET /articles/{id} (auth required)
- ``jobs``     — POST /jobs, GET /jobs/{id}, GET /jobs (auth required)
- ``search``   — POST /search (auth required; Phase-2 stub)
"""
