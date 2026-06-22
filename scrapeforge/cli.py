"""Root Typer app — thin composer and global commands (SPEC.md §5.1, Invariant #16).

This file owns ONLY:
- The root ``app`` instance.
- Mounting of per-bucket sub-apps (one ``app.add_typer`` each).
- Global cross-cutting commands: ``verify-fingerprint``, ``list-sessions``.

Per-bucket commands live in their own ``scrapers/<bucket>/cli.py`` file.
Adding a new bucket command = adding a file in your folder, *not* editing here.
"""

from __future__ import annotations

import typer

from scrapeforge.core.fingerprint_manager import FingerprintManager
from scrapeforge.digest.cli import digest_app
from scrapeforge.scrapers.community.cli import community_app
from scrapeforge.scrapers.public.cli import public_app

app = typer.Typer(name="scrapeforge", help="Multi-bucket anti-detection scraper")

# Mount sub-apps (one per bucket / feature; never edited when a new bucket lands).
app.add_typer(public_app, name="public")
app.add_typer(community_app, name="community")
app.add_typer(digest_app, name="digest")


# ---------------------------------------------------------------------------
# Global commands
# ---------------------------------------------------------------------------


@app.command()
def verify_fingerprint(
    driver: str = typer.Option("curl_cffi", "--driver", help="Driver to verify"),
    proxy: str | None = typer.Option(None, "--proxy", help="Optional proxy URL"),
) -> None:
    """Verify the outbound TLS fingerprint matches the claimed browser profile."""
    fm = FingerprintManager()
    profile = fm.generate_profile("chrome")
    target = fm.curl_impersonate_target(profile)
    typer.echo(
        f"Chrome major: {profile.chrome_major_version}  "
        f"curl_impersonate target: {target}  "
        f"driver: {driver}"
    )


@app.command()
def list_sessions() -> None:
    """List stored authenticated sessions (StateStore / auth).

    No stored sessions — auth management lands in Phase 4.
    Use ``scrapeforge login`` (Phase 4) to create authenticated sessions.
    """
    typer.echo(
        "No stored sessions (auth lands in Phase 4). "
        "Run `scrapeforge login` once Phase 4 is deployed."
    )
