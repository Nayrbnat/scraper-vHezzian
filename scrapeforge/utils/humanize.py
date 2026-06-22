"""Humanization utilities — pure math, no I/O, no sleeping (SPEC.md §4, Invariant #4).

Every function/method here RETURNS a delay value or path; it never calls
``time.sleep`` or any async equivalent.  The callers (drivers/scrapers) decide
when and how to apply the delay.

All randomness comes from the stdlib ``random`` module so tests can control it
deterministically with ``random.seed(...)``.
"""

from __future__ import annotations

import random


class MousePathGenerator:
    """Generate cubic-bezier mouse paths with per-point jitter.

    The path is parameterised as a standard cubic Bezier curve::

        P(t) = (1-t)^3 * P0 + 3(1-t)^2 t * P1 + 3(1-t) t^2 * P2 + t^3 * P3

    where ``P0 = start``, ``P3 = end``, and ``P1``, ``P2`` are control points
    offset from the straight-line midpoints by a random amount.  Small
    independent per-point jitter is added on top of the curve so the path is
    never perfectly smooth.
    """

    # Maximum pixel jitter added per point (independent noise)
    _JITTER_PX: int = 4

    def generate(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int,
    ) -> list[tuple[int, int]]:
        """Return a list of ``(x, y)`` integer pixel coordinates.

        Args:
            start: ``(x, y)`` origin.
            end:   ``(x, y)`` destination.
            duration_ms: nominal path duration; controls point density
                         (~one point per 16 ms frame = 60 fps).

        Returns:
            A non-empty list where ``path[0] == start`` and ``path[-1] == end``.
        """
        n_points = max(2, duration_ms // 16)

        x0, y0 = start
        x3, y3 = end

        # Control points: offset the midpoints perpendicular to the chord.
        mid_x = (x0 + x3) / 2
        mid_y = (y0 + y3) / 2
        span = max(abs(x3 - x0), abs(y3 - y0), 1)
        offset_scale = span * 0.3

        x1 = mid_x + random.uniform(-offset_scale, offset_scale)  # noqa: S311
        y1 = mid_y + random.uniform(-offset_scale, offset_scale)  # noqa: S311
        x2 = mid_x + random.uniform(-offset_scale, offset_scale)  # noqa: S311
        y2 = mid_y + random.uniform(-offset_scale, offset_scale)  # noqa: S311

        path: list[tuple[int, int]] = []
        for i in range(n_points):
            t = i / (n_points - 1)
            u = 1.0 - t
            # Cubic Bezier
            bx = u**3 * x0 + 3 * u**2 * t * x1 + 3 * u * t**2 * x2 + t**3 * x3
            by = u**3 * y0 + 3 * u**2 * t * y1 + 3 * u * t**2 * y2 + t**3 * y3

            if i == 0:
                # First point must equal start exactly — no jitter
                path.append((x0, y0))
            elif i == n_points - 1:
                # Last point must equal end exactly — no jitter
                path.append((x3, y3))
            else:
                jx = random.randint(-self._JITTER_PX, self._JITTER_PX)  # noqa: S311
                jy = random.randint(-self._JITTER_PX, self._JITTER_PX)  # noqa: S311
                path.append((int(round(bx)) + jx, int(round(by)) + jy))

        return path


class ScrollSimulator:
    """Generate human-like scroll event sequences.

    Each element of the returned list is ``(scroll_y_delta, delay_ms)`` where
    ``scroll_y_delta`` is the number of pixels to scroll down and ``delay_ms``
    is the pause to take before the next scroll event.
    """

    # Nominal step range in pixels
    _STEP_MIN_PX: int = 80
    _STEP_MAX_PX: int = 300
    # Jitter applied to each step (±)
    _JITTER_PX: int = 40
    # Delay range in milliseconds between scrolls
    _DELAY_MIN_MS: int = 50
    _DELAY_MAX_MS: int = 300

    def generate_scrolls(
        self,
        page_height: int,
        viewport_height: int,
    ) -> list[tuple[int, int]]:
        """Return scroll events that cover the scrollable area of a page.

        Args:
            page_height:    Total page height in pixels.
            viewport_height: Visible viewport height in pixels.

        Returns:
            List of ``(delta_y_px, delay_ms)`` tuples.  Empty when there is
            nothing to scroll (``page_height <= viewport_height``).
        """
        scrollable = page_height - viewport_height
        if scrollable <= 0:
            return []

        events: list[tuple[int, int]] = []
        accumulated = 0
        while accumulated < scrollable:
            base_step = random.randint(self._STEP_MIN_PX, self._STEP_MAX_PX)  # noqa: S311
            jitter = random.randint(-self._JITTER_PX, self._JITTER_PX)  # noqa: S311
            step = max(1, base_step + jitter)
            # Clamp the last step so we don't overshoot by too much
            remaining = scrollable - accumulated
            step = min(step, remaining + self._JITTER_PX)
            step = max(1, step)
            delay = random.randint(self._DELAY_MIN_MS, self._DELAY_MAX_MS)  # noqa: S311
            events.append((step, delay))
            accumulated += step

        return events


class DelayEngine:
    """Static factory methods that return delay values in seconds.

    Invariant #4: these methods RETURN delays — they never call ``time.sleep``
    or any async equivalent.  The driver/scraper layer decides when to apply them.
    """

    @staticmethod
    def reading_pause() -> float:
        """Return a uniformly distributed reading pause: ``uniform(2.0, 6.0)`` seconds."""
        return random.uniform(2.0, 6.0)  # noqa: S311

    @staticmethod
    def action_delay(min_ms: int = 500, max_ms: int = 2000) -> float:
        """Return a uniformly distributed inter-action delay in seconds.

        Args:
            min_ms: Minimum delay in milliseconds (default 500).
            max_ms: Maximum delay in milliseconds (default 2000).
        """
        return random.uniform(min_ms / 1000.0, max_ms / 1000.0)  # noqa: S311

    @staticmethod
    def typing_interval(mean_ms: float = 80.0, std_ms: float = 20.0) -> float:
        """Return a Gaussian-distributed keypress interval in seconds.

        The value is clamped to ``>= 0`` to avoid negative delays.

        Args:
            mean_ms: Mean keypress interval in milliseconds (default 80).
            std_ms:  Standard deviation in milliseconds (default 20).
        """
        raw_ms = random.gauss(mean_ms, std_ms)  # noqa: S311
        return max(0.0, raw_ms / 1000.0)
