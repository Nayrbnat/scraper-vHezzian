"""Email delivery — a pluggable port with a preview default and an SMTP adapter.

- ``PreviewEmailSender`` (default): writes the rendered HTML to a file and prints a summary.
  Zero credentials — you see exactly what would be sent.
- ``SmtpEmailSender``: real delivery via SMTP (e.g. Gmail). Reads credentials from the
  environment; only used when you opt in. NEVER hard-code or commit credentials.

Same shape as the project's other ports (queue, object store) so production senders
(Resend/SES/etc.) slot in by addition.
"""

from __future__ import annotations

import contextlib
import os
import smtplib
import ssl
import sys
from abc import ABC, abstractmethod
from email.message import EmailMessage
from pathlib import Path

from scrapeforge.digest.render import RenderedEmail


def _safe_print(text: str) -> None:
    """Print UTF-8 text even on a cp1252 Windows console (em-dash, ellipsis, etc.)."""
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8")  # best effort; Python 3.7+
    enc = sys.stdout.encoding or "utf-8"
    sys.stdout.write(text.encode(enc, errors="replace").decode(enc, errors="replace") + "\n")


class EmailSender(ABC):
    """Send a rendered email to one recipient."""

    @abstractmethod
    def send(self, to: str, email: RenderedEmail) -> None: ...


class PreviewEmailSender(EmailSender):
    """Default: render to ``<out_dir>/<slug>.html`` + print a summary. No network, no creds."""

    def __init__(self, out_dir: Path | str = "./output/digests") -> None:
        self.out_dir = Path(out_dir)

    def send(self, to: str, email: RenderedEmail) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        slug = to.replace("@", "_at_").replace(".", "_")
        path = self.out_dir / f"{slug}.html"
        path.write_text(email.html, encoding="utf-8")
        _safe_print("=== Hezzian digest (PREVIEW - not sent) ===")
        _safe_print(f"To:      {to}")
        _safe_print(f"Subject: {email.subject}")
        _safe_print(f"HTML:    {path.resolve()}")
        _safe_print("--- plain text ---")
        _safe_print(email.text)
        _safe_print("=" * 44)


class SmtpEmailSender(EmailSender):
    """Real delivery via SMTP. Reads config from the environment (never committed):

    DIGEST_SMTP_HOST (default smtp.gmail.com), DIGEST_SMTP_PORT (default 587),
    DIGEST_SMTP_USER, DIGEST_SMTP_PASSWORD (a Gmail *app password*), DIGEST_FROM (default USER).
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        user: str | None = None,
        password: str | None = None,
        from_addr: str | None = None,
    ) -> None:
        self.host = host or os.environ.get("DIGEST_SMTP_HOST", "smtp.gmail.com")
        self.port = port or int(os.environ.get("DIGEST_SMTP_PORT", "587"))
        self.user = user or os.environ.get("DIGEST_SMTP_USER", "")
        self.password = password or os.environ.get("DIGEST_SMTP_PASSWORD", "")
        self.from_addr = from_addr or os.environ.get("DIGEST_FROM", self.user)
        if not (self.user and self.password):
            raise ValueError(
                "SMTP credentials missing: set DIGEST_SMTP_USER and DIGEST_SMTP_PASSWORD "
                "(a Gmail app password) in the environment / .env before real sending."
            )

    def send(self, to: str, email: RenderedEmail) -> None:
        msg = EmailMessage()
        msg["Subject"] = email.subject
        msg["From"] = self.from_addr
        msg["To"] = to
        msg.set_content(email.text)
        msg.add_alternative(email.html, subtype="html")
        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(self.user, self.password)
            smtp.send_message(msg)
