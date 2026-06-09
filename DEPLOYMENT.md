# DEPLOYMENT.md — hosting ScrapeForge (single VPS + Docker Compose)

> Target: one Linux VPS running the whole stack via `deployment/docker-compose.yml`. Cheapest, full
> control, and it can run real Chrome + residential proxies (which serverless cannot). Scale workers out
> later. The serving API is stateless, so it can move to a managed/serverless host reading the same
> Postgres if you outgrow one box.

## 1. Topology

| Service | Plane | Image / build | Notes |
|---|---|---|---|
| `caddy` | edge | `caddy:2` | auto-HTTPS, reverse-proxy → `api:8000` |
| `api` | serving | `Dockerfile.api` (slim) | read + enqueue only; **no browser**; scale with `--workers` |
| `worker` | ingestion | `Dockerfile.worker` (Chrome) | consumes jobs, drives browsers, writes Postgres |
| `scheduler` | ingestion | `Dockerfile.worker` | arq cron → enqueues recurring scrapes |
| `nodriver` | ingestion | `services/nodriver_service` | AGPL-isolated; separate process boundary |
| `postgres` | data | `pgvector/pgvector:pg16` | articles + jobs (+ future embeddings) |
| `redis` | data | `redis:7` | job queue (arq) |

## 2. VPS sizing (rough starting point)

Each concurrent Chrome ≈ 0.5–1 GB RAM + ~0.5 vCPU under load. Budget for `COMMUNITY/PUBLIC_MAX_CONCURRENCY`
plus Postgres + Redis + API:

| Workload | vCPU | RAM | Disk |
|---|---|---|---|
| Light (≤3 concurrent browsers) | 2 | 8 GB | 40 GB SSD |
| **Recommended start (≤5 concurrent)** | **4** | **16 GB** | **80 GB SSD** |
| Heavy (≤10, multiple workers) | 8 | 32 GB | 160 GB SSD |

Premium (Bucket 1) runs at concurrency 1 with a ≥60 s rate floor, so it adds little parallel browser load.

## 3. First deploy

```bash
# On the VPS (Docker + compose plugin installed):
git clone https://github.com/Nayrbnat/scraper-vHezzian.git && cd scraper-vHezzian
cp .env.example .env && edit .env          # fill in the secrets below
# place your proxy list where the worker volume expects it:
docker volume create scrapeforge_state-data
# (copy proxies.txt into the state volume — see §6)

docker compose -f deployment/docker-compose.yml --env-file .env up -d --build
docker compose -f deployment/docker-compose.yml exec api alembic upgrade head   # run migrations
docker compose -f deployment/docker-compose.yml ps
```

Validate the compose file before bringing it up:
```bash
docker compose -f deployment/docker-compose.yml --env-file .env config >/dev/null && echo OK
```

## 4. Required secrets (`.env`, never committed)

| Var | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | Postgres superuser password |
| `STATE_STORE_KEY` | Fernet key (32+ char base64) encrypting session state |
| `API_KEYS` | comma-separated API keys clients present to the serving API |
| `API_DOMAIN` | public hostname Caddy issues TLS for |
| `LOG_LEVEL` | INFO/DEBUG/… (optional) |

Generate a Fernet key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## 5. The Bucket 1 interactive-login wrinkle (read this)

Premium sites (FT/Bloomberg/WSJ/Economist) need a **human, headful** SSO login — which a headless VPS
can't do. Two supported options:

1. **Operator login locally (recommended).** Run `scrapeforge login --site ft.com --interactive` on your
   own machine (real Chrome window), complete SSO, which writes an **encrypted** state file
   (`ft.com.enc`). Copy that file into the server's `state-data` volume:
   ```bash
   docker cp ft.com.enc $(docker compose ... ps -q worker):/data/states/ft.com.enc
   ```
   Workers decrypt it with `STATE_STORE_KEY` and reuse the session until TTL expiry, then you repeat.
2. **VNC sidecar.** Run the Camoufox/Chrome VNC variant (`SSOHandler` supports `--vnc`) in a container,
   connect over a tunnel, log in once. Heavier; use only if local login is impractical.

State is **encrypted at rest**; never commit it and never bake it into an image.

## 6. Proxies

Residential proxies live in `proxies.txt` (`protocol://user:pass@host:port`, one per line), mounted via
the `state-data` volume at `/data/proxies.txt` — **not** baked into the image and **not** committed.

## 7. Backups

```bash
# Nightly logical backup of the article/job store:
docker compose ... exec -T postgres pg_dump -U scrapeforge scrapeforge | gzip > backup-$(date +%F).sql.gz
```
Back up the `state-data` volume too (it holds encrypted sessions). Restore: `gunzip -c … | psql`.

## 8. TLS, scaling, observability

- **TLS:** Caddy issues/renews Let's Encrypt certs for `$API_DOMAIN` automatically — just point DNS at
  the VPS and open 80/443.
- **Scale ingestion:** `docker compose ... up -d --scale worker=3`. When one box is saturated, move
  `worker`/`nodriver` to a second host pointed at the same Postgres/Redis (the serving plane is already
  decoupled).
- **Serving elsewhere:** because `api` is stateless and read-mostly, you can later run it on a managed
  platform (or serverless) against a managed Postgres — keep ingestion on the VPS.
- **Observability:** API exposes `/health` and `/ready`; the app emits structured JSON logs (`loguru`)
  and Prometheus metrics (`scrape_count`, `success_rate`, `soft_block_rate`, `rate_limit_wait_seconds`,
  job throughput). Scrape them with any Prometheus/Grafana, or ship logs to your aggregator.

## 9. Security posture

- Private repo; GitHub secret scanning + push protection on.
- Firewall: expose only 80/443 (Caddy). Postgres/Redis stay on the internal compose network.
- Rotate `STATE_STORE_KEY` and `API_KEYS` out of band; revoke a leaked API key by removing it from
  `API_KEYS` and restarting `api`.
- Run `worker`/`api` as non-root (already set in the Dockerfiles).
