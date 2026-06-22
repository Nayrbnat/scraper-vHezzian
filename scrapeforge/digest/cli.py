"""Digest Typer sub-app (Hezzian email prototype).

scrapeforge digest preview --subscriber data/subscribers/dee.json
scrapeforge digest send    --subscriber data/subscribers/dee.json   # real SMTP (needs creds)
"""

from __future__ import annotations

from pathlib import Path

import typer

from scrapeforge.digest.sender import PreviewEmailSender, SmtpEmailSender
from scrapeforge.digest.service import deliver

digest_app = typer.Typer(help="Hezzian personalized email digests (prototype)")


@digest_app.command("preview")
def preview(
    subscriber: Path = typer.Option(  # noqa: B008
        Path("data/subscribers/dee.json"), "--subscriber", "-s", help="Seeded subscriber JSON"
    ),
    source: str = typer.Option("sample", "--source", help="'sample' or 'jsonl:<path>'"),
    out_dir: Path = typer.Option(  # noqa: B008
        Path("./output/digests"), "--out-dir", "-o", help="Where to write the preview HTML"
    ),
) -> None:
    """Build + render the digest and write a preview HTML (no email is sent)."""
    digest = deliver(subscriber, source=source, sender=PreviewEmailSender(out_dir))
    typer.echo(
        f"Built digest for {digest.subscriber_name} <{digest.subscriber_email}>: "
        f"{digest.total_items} item(s) across {len(digest.sections)} section(s)."
    )


@digest_app.command("send")
def send(
    subscriber: Path = typer.Option(  # noqa: B008
        Path("data/subscribers/dee.json"), "--subscriber", "-s"
    ),
    source: str = typer.Option("sample", "--source"),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt"),
) -> None:
    """Send the digest for real via SMTP (requires DIGEST_SMTP_* env / .env)."""
    if not yes:
        typer.confirm("Send a real email via SMTP now?", abort=True)
    try:
        digest = deliver(subscriber, source=source, sender=SmtpEmailSender())
    except ValueError as exc:  # missing credentials
        typer.echo(f"Cannot send: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Sent digest to {digest.subscriber_email} ({digest.total_items} items).")
