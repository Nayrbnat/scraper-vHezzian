# Site Playbook — Substack (public posts, Archive API)
<!-- Target: *.substack.com + custom-domain Substack publications -->
<!-- Researched: 2026-06-22 (researcher agent, training-data draft) -->
<!-- LIVE-VERIFIED: 2026-06-22 by the lead via polite curl against garymarcus.substack.com + -->
<!-- www.noahpinion.blog + astralcodexten.substack.com. Core endpoints/fields confirmed; one -->
<!-- correction applied (see §3 truncated_body_text). -->

> Curated gameplan for `scrapers/community/substack.py` — a new Bucket-2 `CommunityScraper`,
> `curl_cffi` default, same shape as `scrapers/community/reddit.py`. Public posts only (paid =
> deferred to Phase 4 auth). Unit tests use captured JSON fixtures; live = `@integration` only.

## 0. Quick-reference
| Property | Value |
|---|---|
| Auth | None for public posts; paid posts need a logged-in session (DEFER Phase 4) |
| Driver | `curl_cffi` (Chrome impersonation, HTTP/2); follow redirects |
| Anti-bot | **None confirmed** on the public API — LIVE 200s with a browser UA, no Cloudflare/WAF |
| Rate limit | No documented limit; be polite — 2–4 s/req per publication; honor 429 `Retry-After` |
| Paywall signal | **`audience == "only_paid"`** (NOT `truncated_body_text` — see §3) |
| ToS | Moderate risk for commercial/AI-training; low-volume research lower; respect delays |

## 1. Endpoints (LIVE-VERIFIED 2026-06-22)
- **Archive / discovery (primary):** `https://<pub>/api/v1/archive?sort=new&search=&offset=0&limit=12`
  → JSON **array** of post objects, no auth, HTTP 200. `sort=new|top`; `offset` zero-indexed;
  `limit` default 12 (safe ≤25). No cursor — paginate by offset arithmetic.
- **Single post (content):** `https://<pub>/api/v1/posts/<slug>` → full object **including `body_html`**
  for public posts (verified 12.8k-char body), no auth.
- **RSS:** `https://<pub>/feed` — preview-only, ~recent items, no pagination. *Discovery fallback only.*
- **Custom domains:** same paths on the custom domain (e.g. `www.noahpinion.blog/api/v1/archive` works).
  Bare subdomains for custom-domain pubs **301-redirect** to the custom domain → just follow redirects.
- `<pub>` = either `<subdomain>.substack.com` or a custom domain.

## 2. Schema → `Article` mapping (verified field names)
Discovery via archive, then fetch `/posts/<slug>` for the body. From the single-post object:
| `Article` field | JSON path | Notes |
|---|---|---|
| `url` | `canonical_url` | full URL (custom domain if applicable) |
| `title` | `title` | always present |
| `content` | `body_html` | full HTML for public; **clean** (selectolax/strip) before storing |
| `author` | `publishedBylines[0].name` | list (verified); join multiple; fallback `publication.name` |
| `publish_date` | `post_date` | ISO-8601 UTC (`...Z`) → `datetime.fromisoformat(v.replace("Z","+00:00"))` |
| `raw_html` | `body_html` | carried for the claim-check pipeline |
| `metadata.substack_id` | `id` | int |
| `metadata.audience` | `audience` | `everyone` \| `only_paid` |
| `metadata.subtitle` | `subtitle` | nullable |
| `metadata.post_type` | `type` | `newsletter`\|`podcast`\|`thread` |
| `metadata.source_domain` | from `canonical_url` | required (SPEC §2.1) |
| `metadata.bucket` | `"community"` | static |

Archive items carry `title/slug/post_date/audience/canonical_url/type/publishedBylines` (no `body_html`)
— use them to discover + pre-filter, then fetch the body per public slug.

## 3. Public vs paid (the soft-block rule) — CORRECTED
- **Primary signal: `audience == "only_paid"`** → paywalled. `"everyone"` → public.
- ⚠️ **`truncated_body_text` is NOT a paywall signal** — live data shows it's present (a preview snippet)
  even on **public** posts. Do NOT use it to detect paywalls (the training-data draft was wrong here).
- **Secondary heuristic:** a paid post's `body_html` is truncated → `len(clean_text(body_html)) <
  MIN_CONTENT_LENGTH (500)` ⇒ treat as paywalled.
- **Contract:** skip paid posts. Pre-filter in the archive loop (`audience != "everyone"` → skip);
  re-check after fetch. Never return a truncated stub as `success` — set `ScrapeResult.status="error"`,
  `error="paywalled"` (Invariant #15: a paywall stub is a soft block, not a success).

## 4. Anti-bot posture
No Cloudflare/Imperva/Turnstile on the public API (LIVE 200s, browser UA). No hard rate limit documented;
community practice ≤30 req/min. Default driver `curl_cffi` with the Chrome-aligned impersonate; the engine's
`RateLimiter` provides 2–4 s/pub politeness; 429 → exponential backoff (start 30 s); 3× 429 → trip
`CircuitBreaker`. No proxy strictly required for public content, but route production through `ProxyRotator`.
Escalation to `patchright` (paid content via injected auth) is **deferred to Phase 2/4** — not in this scraper.

## 5. Pagination — `scrape_publication(pub, limit, sort='new')`
Offset-based. Loop `/api/v1/archive?...&offset=N&limit=12`; per page: skip `audience!='everyone'`, fetch
`/posts/<slug>` for the rest, build Articles. Stop when: array empty, OR fewer items than requested
(last page), OR `limit` reached. Increment `offset` by **items returned** (not page size). Normalize a bare
subdomain (`"semianalysis"`) to `https://semianalysis.substack.com`; accept full domains too.

## 6. Open / unverified (re-check at build time)
Confirmed live: archive + single-post endpoints, no-auth, fields (`title/slug/post_date/audience/
canonical_url/body_html/publishedBylines`), custom-domain behavior + 301 redirect, no WAF. Still unverified:
`limit>25` cap behavior; 429 thresholds from a datacenter IP at volume; a real `only_paid` post's truncated
`body_html` shape (have a public sample, not a paid one — handle defensively); multi-author `publishedBylines`.
HTML-page CSS selectors (fallback) are unverified and brittle — prefer the JSON API.

## 7. Builder summary
1. **Endpoints:** `/api/v1/archive?sort=new&offset=N&limit=12` (discover) → `/api/v1/posts/<slug>` (body). No RSS/HTML for Phase 1.
2. **Map:** `title←title`, `content←clean(body_html)`, `author←publishedBylines[0].name`, `publish_date←post_date (ISO-UTC)`, `url←canonical_url`; stash `audience/id/subtitle/type` in metadata.
3. **Public-only:** pre-filter `audience=='everyone'` in the archive loop; post-fetch re-check `audience=='only_paid'` OR clean-body < 500 → `status='error', error='paywalled'`, skip.
4. **Paginate:** offset loop; stop on empty / short page / limit; offset += items returned.
5. **Driver:** `curl_cffi` (impersonate from installed Chrome), follow redirects, rely on `RateLimiter` for politeness; per-module `SubstackSettings` fragment; self-register `@register_scraper` for `*.substack.com` (+ configured custom domains).

## 8. Sources
Live: garymarcus.substack.com, www.noahpinion.blog, astralcodexten.substack.com (`/api/v1/archive`,
`/api/v1/posts/<slug>`), 2026-06-22. Plus community documentation of the Substack API v1 (archive/posts
endpoints, `audience` field, offset pagination) cross-checked against the live responses.
