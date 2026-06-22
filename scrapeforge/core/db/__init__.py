"""Datastore package for ScrapeForge (W3 — SPEC.md §3.21).

Three sub-modules with clear SRP boundaries:

- ``models``       — SQLAlchemy 2.0 async ORM definitions (``Article``, ``Job``, ``Source``).
- ``session``      — Engine + sessionmaker factory (injectable; no I/O at import time).
- ``repositories`` — Typed async query functions used by the API and workers.

Import example::

    from scrapeforge.core.db.models import Article, Job
    from scrapeforge.core.db.session import make_engine, make_sessionmaker
    from scrapeforge.core.db import repositories as repo
"""
