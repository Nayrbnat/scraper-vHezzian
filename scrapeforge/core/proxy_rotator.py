"""Proxy lifecycle management for ScrapeForge (SPEC.md §3.14, Invariant #7).

Responsibilities
----------------
- Parse a proxy list file (``protocol://user:pass@host:port``).
- Health-check proxies on demand via curl_cffi ``AsyncSession``.
- Return the first healthy proxy matching optional filters.
- Track burned proxies with a cooldown period.
- Release proxies back to the pool after use.

Invariant #7 — one proxy per bridge
    This class only *provides* healthy proxies; the engine binds one proxy to
    one ``StealthBridge`` and never rotates mid-session.  Rotation between
    requests is the engine's responsibility.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from curl_cffi.requests import AsyncSession
from pydantic import ValidationError

from scrapeforge.config.settings import Settings
from scrapeforge.core.models import ProxySession
from scrapeforge.exceptions import ProxyError  # noqa: F401 — re-exported for callers

log = logging.getLogger(__name__)

_DEFAULT_HEALTH_URL = "https://httpbin.org/ip"
_DEFAULT_COOLDOWN_MINUTES = 60


class ProxyRotator:
    """Manages proxy lifecycle with health checks and session affinity.

    Parameters
    ----------
    proxy_list_path:
        Path to a text file of proxy URLs, one per line.  Lines starting with
        ``#`` (after stripping) and blank lines are ignored.  If ``None``, the
        path is read from ``Settings().PROXY_LIST_PATH``; note that ``Settings``
        requires ``STATE_STORE_KEY`` in the environment — pass an explicit path
        in tests to avoid that dependency.

    Invariants
    ----------
    - One proxy = one bridge (Invariant #7): never rotate mid-session.
    - Failed proxies are marked ``'unhealthy'``; deliberately burned proxies
      are marked ``'burned'`` and excluded for ``_cooldown_minutes``.

    Notes
    -----
    ``ProxySession`` uses ``slots=True`` which prevents adding ad-hoc attributes.
    Burn timestamps are tracked in ``_burn_times: dict[int, datetime]`` keyed by
    ``id(proxy)`` — this is safe for the rotator's lifetime because
    ``ProxySession`` objects in ``self.proxies`` are never removed or replaced
    (a future ``burned_at`` field on the model would retire this side-channel).

    Settings are resolved exactly once in ``__init__`` (catching only
    ``pydantic.ValidationError`` so genuine config bugs still surface) and
    stored as ``self._health_url`` and ``self._cooldown_minutes``.
    """

    def __init__(self, proxy_list_path: Path | None = None) -> None:
        self.proxies: list[ProxySession] = []
        # Keyed by id(ProxySession); safe because proxies in self.proxies are
        # never removed/replaced for this rotator's lifetime.
        self._burn_times: dict[int, datetime] = {}

        # Resolve Settings once — catch only ValidationError so real bugs surface.
        try:
            _settings = Settings()
            self._health_url: str = _settings.PROXY_HEALTH_CHECK_URL
            self._cooldown_minutes: int = _settings.PROXY_BURNED_COOLDOWN_MINUTES
            path = proxy_list_path if proxy_list_path is not None else _settings.PROXY_LIST_PATH
        except ValidationError:
            self._health_url = _DEFAULT_HEALTH_URL
            self._cooldown_minutes = _DEFAULT_COOLDOWN_MINUTES
            path = proxy_list_path if proxy_list_path is not None else Path("./proxies.txt")

        self._load_proxies(path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_healthy_proxy(
        self,
        country_code: str | None = None,
        exclude_burned: bool = True,
    ) -> ProxySession | None:
        """Return the first healthy proxy matching the given criteria.

        The method health-checks candidates in list order and returns the first
        one that passes.  It stamps ``last_used`` on the returned proxy.

        Parameters
        ----------
        country_code:
            If given, only consider proxies whose ``country_code`` matches.
        exclude_burned:
            If ``True`` (default), skip proxies whose ``health_status`` is
            ``'burned'`` and whose burn cooldown has not yet expired.

        Returns
        -------
        ProxySession | None
            The first healthy proxy, or ``None`` if none is available.
        """
        candidates = self._filter_candidates(
            country_code=country_code, exclude_burned=exclude_burned
        )

        for proxy in candidates:
            healthy = await self.health_check(proxy)
            if healthy:
                proxy.last_used = datetime.now(UTC)
                return proxy

        return None

    async def health_check(self, proxy: ProxySession) -> bool:
        """Test *proxy* by making one async GET to ``self._health_url``.

        Uses ``async with AsyncSession(...)`` to guarantee the connection pool
        is closed after every check, preventing resource leaks in the
        ``get_healthy_proxy`` loop.

        Updates ``proxy.health_status`` and ``proxy.failure_count`` in place.

        Returns
        -------
        bool
            ``True`` iff the response status is 200.
        """
        try:
            async with AsyncSession(proxy=proxy.url) as session:
                response = await session.get(self._health_url)
            if response.status_code == 200:
                proxy.health_status = "healthy"
                return True
            proxy.health_status = "unhealthy"
            proxy.failure_count += 1
            return False
        except Exception as exc:  # noqa: BLE001
            log.debug("Proxy %s health check failed: %s", proxy.url, exc)
            proxy.health_status = "unhealthy"
            proxy.failure_count += 1
            return False

    def mark_burned(self, proxy: ProxySession) -> None:
        """Mark *proxy* as burned.

        Burned proxies are excluded from ``get_healthy_proxy`` until the
        cooldown period (``self._cooldown_minutes``) expires.

        The burn timestamp is stored in ``self._burn_times`` (keyed by
        ``id(proxy)``) rather than on the ``ProxySession`` itself, because
        ``ProxySession`` uses ``__slots__`` which prevents adding new attributes.
        """
        proxy.health_status = "burned"
        self._burn_times[id(proxy)] = datetime.now(UTC)

    def release(self, proxy: ProxySession) -> None:
        """Release *proxy* back to the pool.

        Clears ``assigned_scraper`` and stamps ``last_used``.
        """
        proxy.assigned_scraper = None
        proxy.last_used = datetime.now(UTC)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_proxies(self, path: Path) -> None:
        """Parse *path* into ``self.proxies``.

        File format: one ``protocol://[user:pass@]host:port`` per line.
        Blank lines and lines whose first non-whitespace character is ``#``
        are silently skipped.  A missing file logs a warning and leaves
        ``self.proxies`` empty (no crash).
        """
        if not path.exists():
            log.warning("Proxy list file not found: %s — running with no proxies.", path)
            return

        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            self.proxies.append(ProxySession(url=line))

        log.debug("Loaded %d proxies from %s.", len(self.proxies), path)

    def _filter_candidates(
        self,
        country_code: str | None,
        exclude_burned: bool,
    ) -> list[ProxySession]:
        """Return proxies that pass the given filters (no health check yet)."""
        result: list[ProxySession] = []
        for proxy in self.proxies:
            if exclude_burned and proxy.health_status == "burned":
                if not self._burn_cooldown_expired(proxy):
                    continue
                # Cooldown has passed — allow the proxy back into rotation and
                # remove the stale entry to prevent unbounded dict growth.
                proxy.health_status = "unknown"
                self._burn_times.pop(id(proxy), None)

            if country_code is not None and proxy.country_code != country_code:
                continue

            result.append(proxy)
        return result

    def _burn_cooldown_expired(self, proxy: ProxySession) -> bool:
        """Return ``True`` if the proxy's burn cooldown has elapsed."""
        burned_at = self._burn_times.get(id(proxy))
        if burned_at is None:
            return True  # no timestamp — treat as expired (defensive)

        return datetime.now(UTC) - burned_at >= timedelta(minutes=self._cooldown_minutes)
