# ScrapeForge

Multi-bucket anti-detection scraper with a serving API. Two decoupled planes over a shared Postgres:

- **Ingestion plane** (heavy): scrapers + real Chrome (patchright/nodriver) + curl_cffi/primp + residential
  proxies → `ScrapeEngine` → `PostgresSink`. Long-running; runs on a worker host.
- **Serving plane** (light): a FastAPI **read + enqueue** API over the datastore — `GET /articles`,
  `GET /jobs`, `POST /jobs`. Stateless; never drives a browser.

```
scrapers + Chrome + proxies → ScrapeEngine → PostgresSink ─ Postgres(+pgvector) ─ FastAPI read/jobs API
        ▲ workers consume jobs                                                          │
        └────────────────────── Redis queue ◀───────────────────────────────────────── ┘ (API enqueues)
```

## Documentation
- **`SPEC.md`** — class contracts, object graph, invariants (the source of truth).
- **`architecture.MD`** — directory tree, module map, data flows, serving plane.
- **`planning.MD`** — phased roadmap.
- **`CLAUDE.md`** — shared standards every contributor/agent follows.
- **`GitHub.md`** — branching, worktrees, CI, the agent-team workflow.
- **`TESTING.md`** — test strategy + per-module test matrix (TDD).
- **`DEPLOYMENT.md`** — single-VPS + Docker Compose hosting runbook.

## Quick start (dev)
```bash
uv pip install -e ".[dev,test]"
ruff check . && ruff format --check .
pytest -m "not integration" --cov
```

> ⚠️ Scrapes paywalled/community sources — keep the repo private; never commit `.env`, proxies, cookies,
> or scraped output (see `.gitignore`).
