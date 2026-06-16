"""Tests for scrapeforge.exceptions — the typed error hierarchy.

Every subclass of ScrapeForgeError must:
- be caught by a bare ``except ScrapeForgeError`` handler, and
- remain distinct from all other subclasses.

TDD: these tests are written before the implementation exists; running them should fail
with ImportError until scrapeforge/exceptions.py is created.
"""

from __future__ import annotations

import pytest

from scrapeforge.exceptions import (
    AuthError,
    ChallengeError,
    DriverError,
    FingerprintError,
    ProxyError,
    RateLimitError,
    ScrapeForgeError,
)

# ---------------------------------------------------------------------------
# All concrete subclasses under test
# ---------------------------------------------------------------------------
ALL_SUBCLASSES = [
    DriverError,
    AuthError,
    ProxyError,
    ChallengeError,
    RateLimitError,
    FingerprintError,
]


@pytest.mark.parametrize("exc_class", ALL_SUBCLASSES)
def test_subclass_is_instance_of_base(exc_class: type) -> None:
    """Raising any ScrapeForge error must be catchable as ScrapeForgeError."""
    instance = exc_class("test message")
    assert isinstance(instance, ScrapeForgeError), (
        f"{exc_class.__name__} must be a subclass of ScrapeForgeError"
    )


@pytest.mark.parametrize("exc_class", ALL_SUBCLASSES)
def test_subclass_is_exception(exc_class: type) -> None:
    """All errors must also be catchable as built-in Exception."""
    instance = exc_class("test message")
    assert isinstance(instance, Exception)


@pytest.mark.parametrize("exc_class", ALL_SUBCLASSES)
def test_subclass_carries_message(exc_class: type) -> None:
    """Error message must be preserved on the raised instance."""
    msg = f"error from {exc_class.__name__}"
    instance = exc_class(msg)
    assert str(instance) == msg


@pytest.mark.parametrize("exc_class", ALL_SUBCLASSES)
def test_caught_by_base_handler(exc_class: type) -> None:
    """A bare ``except ScrapeForgeError`` must catch every subclass."""
    caught = False
    try:
        raise exc_class("boom")
    except ScrapeForgeError:
        caught = True
    assert caught, f"{exc_class.__name__} was not caught by ScrapeForgeError handler"


def test_subclasses_are_distinct() -> None:
    """Each error type is a distinct class — no accidental aliasing."""
    for i, cls_a in enumerate(ALL_SUBCLASSES):
        for cls_b in ALL_SUBCLASSES[i + 1 :]:
            assert cls_a is not cls_b, f"{cls_a.__name__} and {cls_b.__name__} are the same object"


@pytest.mark.parametrize(
    ("raise_class", "catch_class"),
    [
        (ChallengeError, ProxyError),
        (ProxyError, ChallengeError),
        (DriverError, AuthError),
        (AuthError, DriverError),
        (RateLimitError, FingerprintError),
        (FingerprintError, RateLimitError),
    ],
)
def test_subclasses_do_not_cross_catch(raise_class: type, catch_class: type) -> None:
    """One subclass must NOT be caught by a handler for a sibling subclass."""
    cross_caught = False
    try:
        raise raise_class("cross-catch test")
    except catch_class:  # type: ignore[misc]
        cross_caught = True
    except ScrapeForgeError:
        pass
    assert not cross_caught, (
        f"{raise_class.__name__} should NOT be caught by an {catch_class.__name__} handler"
    )
