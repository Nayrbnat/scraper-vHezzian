# Site Playbook — Reddit (Public JSON API)
<!-- Target: www.reddit.com / old.reddit.com -->
<!-- Last researched: 2026-06-16 -->
<!-- Researcher: ScrapeForge researcher agent (read-only recon) -->

> Curated gameplan for `scrapers/community/reddit.py`. Default driver `curl_cffi` (no browser, no
> login). Build-time unit tests use a **captured `.json` fixture** (deterministic, in CI); the one live
> `.json` smoke is polite/low-volume/manual only. Confidence + last-verified tags are per claim.

---

## 0. Quick-Reference

| Property | Value |
|---|---|
| Target | `www.reddit.com` (primary), `old.reddit.com` (comment fallback) |
| Auth required | No (public `.json`; OAuth only for write ops or private subs) |
| Recommended driver | `curl_cffi` (Chrome impersonation, HTTP/2) |
| Escalation driver | `patchright` (JS-rendered comment trees — deferred to Phase 2+) |
| Politeness interval | 4–6 s between requests (see §5) |
| Datacenter IP | High block risk; residential proxy strongly recommended for sustained runs |
| Anti-bot layer | UA-gating + rate-limit (429); no confirmed Cloudflare/Imperva on `.json` |
| Rate limit (unauth) | ~10 req/min observed by practitioners; ~60 req/min with OAuth |
| ToS risk | HIGH for commercial/AI-training use; moderate for research/personal low-volume |

---

## 1. Endpoints

### 1.1 Subreddit Listings

`.json` appended to a standard Reddit URL returns raw JSON without OAuth. No API key for public subs.

```
Base (default sort = hot):
  https://www.reddit.com/r/<sub>/.json

Sort variants:
  https://www.reddit.com/r/<sub>/hot.json
  https://www.reddit.com/r/<sub>/new.json
  https://www.reddit.com/r/<sub>/top.json?t=<window>       # window: hour|day|week|month|year|all
  https://www.reddit.com/r/<sub>/rising.json
  https://www.reddit.com/r/<sub>/controversial.json?t=<window>

Query parameters (all listing endpoints):
  ?limit=N        — items per page; default 25, max 100
  ?after=<name>   — fullname of last item seen (e.g. "t3_abc123"); drives forward pagination
  ?before=<name>  — reverse pagination (rarely needed)
  ?count=N        — total items fetched so far; increment by items-returned each page
```

Confidence: HIGH — cross-confirmed (Simon Willison TIL, JCChouinard, ScrapFly, Educative).
Last verified: 2026-06-16.

### 1.2 Single Post + Comments

```
  https://www.reddit.com/comments/<post_id>.json
  https://www.reddit.com/r/<sub>/comments/<post_id>/<slug>.json

  ?limit=N   — top-level comments (max 500 on old.reddit.com)
  ?sort=<s>  — best|top|new|controversial|old|qa
  ?depth=N   — max comment tree depth
```

IMPORTANT: returns a **2-element JSON array**, not a single object:
- `[0]`: Listing with the single post (one `t3` child)
- `[1]`: Listing with top-level comments (`t1` children; `kind="more"` = collapsed, needs `/api/morechildren`, out of scope)

Confidence: HIGH. Last verified: 2026-06-16.

### 1.3 Domain Feed (bonus)
```
  https://www.reddit.com/domain/<domain>/.json?sort=new&limit=100
```

### 1.4 old.reddit.com vs www.reddit.com
`.json` works on both with identical schema. `old.reddit.com` is more static (lower anti-bot exposure,
`?limit=500` on comments observed). `www.reddit.com` uses heavier JS (`/svc/shreddit/` SPA endpoints —
avoid for scraping). Confidence: HIGH (JSON parity), MEDIUM (old.reddit limit=500). Last verified: 2026-06-16.

### 1.5 Out of scope (Phase 1)
`/api/morechildren` (JS-gated), `/svc/shreddit/...` (SPA), OAuth (`oauth.reddit.com`), search.

---

## 2. JSON Schema — Field Paths

### 2.1 Listing envelope (`kind="Listing"`)
```
.data.dist        → int (children count this page)
.data.after       → string | null   ← PAGINATION CURSOR (fullname of last child)
.data.before      → string | null
.data.children    → array of Thing objects
```

### 2.2 Post object (`kind="t3"`) → Article mapping
```
.data.id               → "abc123"
.data.name             → "t3_abc123"  (fullname; use as `after` cursor)
.data.title            → ← Article.title
.data.selftext         → ← Article.content   (empty "" for link posts)
.data.selftext_html    → HTML-escaped HTML of selftext (secondary)
.data.url              → link-post destination
.data.author           → ← Article.author   ("[deleted]" if removed)
.data.created_utc      → float epoch → Article.publish_date
                          datetime.fromtimestamp(v, tz=timezone.utc)
.data.permalink        → "/r/<sub>/comments/<id>/<slug>/" → Article.url
                          (prepend "https://www.reddit.com")
.data.subreddit        → metadata["subreddit"]
.data.score            → metadata["score"]
.data.num_comments     → metadata["num_comments"]
.data.is_self          → bool (True = text post; False = link post)
.data.over_18          → NSFW flag
.data.removed_by_category → null = live; "moderator"/"author"/... = removed
```

Content rule: if `is_self` and `selftext` not empty/`[removed]`/`[deleted]`, use `selftext`. For link
posts (`is_self=False`), content is empty; store `url` in `metadata["link_url"]`.

Confidence: HIGH (field names stable since ~2020, 4+ sources). Last verified: 2026-06-16.

### 2.3 Comment object (`kind="t1"`) — deferred (Phase 2+)
```
.data.body, .data.body_html, .data.author, .data.created_utc,
.data.parent_id (t3_/t1_ fullname), .data.link_id (t3_), .data.score,
.data.replies (Listing | ""), .data.depth (0 = top-level)
```

### 2.4 Single-post 2-element array
```python
post = response_json[0]["data"]["children"][0]["data"]
comments = response_json[1]["data"]["children"]  # kind "t1" | "more"
```

---

## 3. Authentication

- **Phase 1 (what we use):** none. Public `.json` returns full public post data, rate-limited by
  IP + User-Agent.
- **OAuth (out of scope):** required for private subs / higher limits / write. `client_credentials`
  → Bearer token; 60–100 req/min. Reddit's Responsible Builder Policy (Nov 2025) gates new OAuth apps.
- The unauthenticated `.json` path has no formal approval gate, but is governed by the User Agreement +
  robots.txt (see §6). Confidence: MEDIUM. Last verified: 2026-06-16.

---

## 4. Anti-Bot Posture (critical)

### 4.1 CDN/WAF
No confirmed Cloudflare/Turnstile/Imperva on `.json` (Reddit uses Fastly + own rate-limiting). No CAPTCHA
reported on `.json`. Confidence: MEDIUM (not tested from here — verify at implementation). Last verified: 2026-06-16.

### 4.2 User-Agent gating (HIGH severity)
A missing/generic UA (e.g. `python-urllib`) → immediate `{"message":"Too Many Requests","error":429}`.
Hard gate. Need a realistic browser UA matching the curl_cffi impersonation target's TLS fingerprint, e.g.
`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36`.
Also send `Accept: application/json...`, `Accept-Language: en-US,en;q=0.9`, `Accept-Encoding: gzip, deflate, br`.
UA MUST be coherent with the `BrowserProfile` (Invariant #11). Confidence: HIGH (Willison + ScrapFly). Last verified: 2026-06-16.

### 4.3 Rate limits
Unauth: ~10 req/min per IP (community-reported); hard 429; `Retry-After` may be present (honor it).
OAuth: 60–100 req/min (out of scope). On 429: exponential backoff from 10 s; 3 consecutive 429s → trip
CircuitBreaker for the domain. Confidence: MEDIUM (the ~10/min figure), HIGH (429 shape). Last verified: 2026-06-16.

### 4.4 Datacenter IP blocking
Datacenter ranges (AWS/GCP/Azure/DO) face elevated blocks; community reports IP flagging after ~50 pages.
**Residential proxy via ProxyRotator for production.** Datacenter OK for dev / single-shot validation.
Soft-block/200-decoy NOT confirmed for Reddit `.json` (hard 429/403 instead); if HTTP 200 but
`data.dist == 0` on a known-nonempty sub, treat as suspicious. Confidence: MEDIUM (datacenter risk), LOW
(200-decoy absence — unverified). Last verified: 2026-06-16.

### 4.5 Recommended driver stack
- **Phase 1:** `curl_cffi` `impersonate="chrome131"` (or latest supported), 4–6 s between requests,
  residential proxy. No escalation needed for public `.json`.
- **Phase 2+:** `patchright` for `/api/morechildren` + shreddit SPA comment pagination (deferred).

---

## 5. Pagination strategy for `scrape_subreddit(sub, limit, sort='new')`

```python
collected, after, count = [], None, 0
while len(collected) < limit:
    page_size = min(100, limit - len(collected))
    params = {"limit": page_size, "count": count}
    if after:
        params["after"] = after
    resp = await bridge.navigate(f"https://www.reddit.com/r/{sub}/{sort}.json?...")  # 429→backoff; 403→raise
    data = json.loads(html)["data"]
    children = data["children"]
    if not children:
        break
    collected += [_parse_post(c["data"]) for c in children if c["kind"] == "t3"]
    after = data["after"]            # None ⇒ end of listing
    count += len(children)
    if after is None:
        break
    await asyncio.sleep(interval)    # RateLimiter handles this in the engine
return collected[:limit]
```
Rules: ≤100/page; increment `count` by items-returned; `after == null` ⇒ done; `data.dist == 0` on a
nonempty sub ⇒ treat as soft-block.

---

## 6. Open risks / unverified / ToS

**Unverified (test live at implementation):** `.json` from datacenter IP (may 403/429 pre-UA);
`Retry-After` presence; exact rate-limit window; 200-decoy absence; `old.reddit` `limit=500`; NSFW
`over18=1` cookie; quarantined-sub handling; `removed_by_category` stability.

**ToS/legal:** Reddit User Agreement prohibits overburdening scraping; Responsible Builder Policy
(Nov 2025) prohibits selling/commercializing data and ML-training use without approval; Reddit has sued
commercial scrapers (Perplexity Oct 2025; Anthropic Jun 2025). Research/personal low-volume = moderate
risk; commercial = HIGH without a Data API agreement. robots.txt disallows many crawlers. Confidence: HIGH
(prohibition language), MEDIUM (low-volume enforcement). Last verified: 2026-06-16.

**Landscape watch:** the Nov-2025 policy may precede gating unauthenticated `.json` (API key, WAF, or
empty listings for non-browser TLS).

---

## 7. Builder summary (concrete recommendation)

1. `curl_cffi` `impersonate="chrome131"` (or latest) + a matching Chrome UA from `BrowserProfile` —
   Reddit gates on UA **and** TLS fingerprint; mismatches get 429.
2. Route production through a residential proxy via `ProxyRotator`; datacenter IPs flag after ~50 pages.
3. Paginate with `after=<data.after>` and `count=<running_total>`; cap `limit=100`/page; stop when
   `after` is null or `children` empty.
4. Map: `title←data.title`, `content←data.selftext` (empty for link posts),
   `author←data.author`, `publish_date←datetime.fromtimestamp(data.created_utc, tz=UTC)`,
   `url←"https://www.reddit.com"+data.permalink`, cursor `←data.name`.
5. Enforce ~6 s inter-request delay (`RateLimiter`, ~10/min); 429 → exponential backoff (start 10 s);
   3 consecutive 429s → trip `CircuitBreaker`.

---

## 8. Sources (cross-check record)
- Simon Willison TIL — https://til.simonwillison.net/reddit/scraping-reddit-json
- ScrapFly — https://scrapfly.io/blog/posts/how-to-scrape-reddit-social-data
- JCChouinard — https://www.jcchouinard.com/documentation-on-reddit-apis-json/
- Educative — https://www.educative.io/courses/getting-started-with-the-reddit-api-in-javascript/read-posts-and-comments
- PainOnSocial (rate limits) — https://painonsocial.com/blog/reddit-api-rate-limits-guide
- Data365 (API limits) — https://data365.co/blog/reddit-api-limits
- DEV.to (2025 scraping reality) — https://dev.to/short_playskits_ab152535/i-tried-scraping-reddit-in-2025-heres-what-happens-when-you-fight-the-api-57o5
- DEV.to (2026 methods) — https://dev.to/agenthustler/how-to-scrape-reddit-in-2026-3-methods-that-still-work-402b
- ReplyDaddy (Responsible Builder Policy) — https://replydaddy.com/blog/reddit-api-pre-approval-2025-personal-projects-crackdown
- BrightData (curl_cffi) — https://brightdata.com/blog/web-data/web-scraping-with-curl-cffi
