"""Digest Typer sub-app (Hezzian email prototype).

scrapeforge digest preview --subscriber data/subscribers/dee.json
scrapeforge digest send    --subscriber data/subscribers/dee.json   # real SMTP (needs creds)
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import typer

from scrapeforge.digest.sender import PreviewEmailSender, SmtpEmailSender
from scrapeforge.digest.service import deliver

digest_app = typer.Typer(help="Hezzian personalized email digests (prototype)")


def _load_dotenv() -> None:
    """Best-effort load of ``.env`` so DIGEST_* vars are picked up (python-dotenv ships with
    pydantic-settings). Never fails if it's unavailable."""
    with contextlib.suppress(Exception):
        from dotenv import load_dotenv

        load_dotenv()


@digest_app.command("preview")
def preview(
    subscriber: Path = typer.Option(  # noqa: B008
        Path("data/subscribers/dee.json"), "--subscriber", "-s", help="Seeded subscriber JSON"
    ),
    source: str = typer.Option(
        "sample",
        "--source",
        help="'sample', 'jsonl:<path>', or 'postgres' (relevance-ranked from the DB)",
    ),
    out_dir: Path = typer.Option(  # noqa: B008
        Path("./output/digests"), "--out-dir", "-o", help="Where to write the preview HTML"
    ),
) -> None:
    """Build + render the digest and write a preview HTML (no email is sent)."""
    _load_dotenv()
    to = os.environ.get("DIGEST_TO") or None
    digest = deliver(subscriber, source=source, sender=PreviewEmailSender(out_dir), to=to)
    typer.echo(
        f"Built digest for {digest.subscriber_name} <{digest.subscriber_email}>: "
        f"{digest.total_items} item(s) across {len(digest.sections)} section(s)."
    )


@digest_app.command("send")
def send(
    subscriber: Path = typer.Option(  # noqa: B008
        Path("data/subscribers/dee.json"), "--subscriber", "-s"
    ),
    source: str = typer.Option(
        "sample",
        "--source",
        help="'sample', 'jsonl:<path>', or 'postgres' (relevance-ranked from the DB)",
    ),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Send the digest for real via SMTP (requires DIGEST_SMTP_* env / .env)."""
    _load_dotenv()
    to = os.environ.get("DIGEST_TO") or None
    recipient = to or "the subscriber's email"
    if not yes:
        typer.confirm(f"Send a real email via SMTP to {recipient} now?", abort=True)
    try:
        digest = deliver(subscriber, source=source, sender=SmtpEmailSender(), to=to)
    except ValueError as exc:  # missing credentials
        typer.echo(f"Cannot send: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    sent_to = to or digest.subscriber_email
    typer.echo(f"Sent digest to {sent_to} ({digest.total_items} items).")
