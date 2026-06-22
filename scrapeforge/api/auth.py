"""API authentication and per-key rate limiting (SPEC.md §3.22).

Auth strategy: ``X-API-Key`` header validated against ``Settings.api_key_set()``.
Fail closed: if no keys are configured, ALL requests are rejected with 401.

Rate limiting: simple in-process per-key bucket.
- Bucket key: ``(api_key, minute_bucket)`` where ``minute_bucket = int(time.time() // 60)``.
- Counter stored in ``app.state.rate_limit_counters`` (a plain dict, NOT thread-safe by design —
  this is async single-threaded; a distributed limiter is a later phase).
- Exceeding ``Settings.API_RATE_LIMIT_PER_MIN`` raises HTTP 429.
- Stale entries (minute bucket < current) are pruned on each access to bound memory growth.

Note: ``time.time()`` is intentional here — we need real wall-clock minutes,
not an injectable clock.
"""

from __future__ import annotations

import time

from fastapi import Header, HTTPException, Request

from scrapeforge.api.deps import get_settings


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: validate ``X-API-Key`` header and enforce per-key rate limit.

    Args:
        request:   The current FastAPI ``Request`` (gives access to ``app.state``).
        x_api_key: Value of the ``X-API-Key`` header (``None`` if absent).

    Returns:
        The validated API key string.

    Raises:
        HTTPException(401): Key is missing or not in the configured key set.
        HTTPException(429): Key has exceeded ``API_RATE_LIMIT_PER_MIN`` in the current minute.
    """
    settings = get_settings(request)
    valid_keys = settings.api_key_set()

    # Fail closed: reject if no keys configured OR key not in the set
    if not valid_keys or x_api_key not in valid_keys:
        raise HTTPException(status_code=401, detail="invalid or missing API key")

    # Per-key rate limit (in-process, per-minute bucket)
    minute_bucket = int(time.time() // 60)
    bucket_key = (x_api_key, minute_bucket)
    counters: dict[tuple[str, int], int] = request.app.state.rate_limit_counters

    # Prune stale entries (minute bucket < current) to prevent unbounded memory growth.
    # A stale entry is any entry whose minute bucket is not the current one — it can
    # never be matched again because time only moves forward.
    stale_keys = [k for k in counters if k[1] != minute_bucket]
    for k in stale_keys:
        del counters[k]

    current_count = counters.get(bucket_key, 0)
    if current_count >= settings.API_RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="rate limit exceeded")

    counters[bucket_key] = current_count + 1
    return x_api_key
