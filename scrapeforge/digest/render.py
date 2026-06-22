"""Render a ``Digest`` into a deliverable email (subject + HTML + plain-text).

Inline styles only (email clients strip <style> blocks). The HTML and text are kept in sync so
preview and real delivery look the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape

from scrapeforge.digest.models import Digest

_BRAND = "Hezzian"


@dataclass(frozen=True, slots=True)
class RenderedEmail:
    """A ready-to-send email payload."""

    subject: str
    html: str
    text: str


def _subject(digest: Digest) -> str:
    n = digest.total_items
    return f"{_BRAND} {digest.cadence_label} — {digest.period} ({n} update{'s' if n != 1 else ''})"


def render_text(digest: Digest) -> str:
    lines = [f"{_BRAND} — {digest.period}", "", f"Hi {digest.subscriber_name},", ""]
    if digest.is_empty:
        lines += ["No new updates matched your preferences today.", ""]
    for section in digest.sections:
        lines.append(section.heading.upper())
        for item in section.items:
            tag = f"  [{', '.join(item.matched_on)}]" if item.matched_on else ""
            lines.append(f"- {item.title}{tag}")
            lines.append(f"  {item.source} — {item.url}")
            lines.append(f"  {item.summary}")
            lines.append("")
        lines.append("")
    lines += ["—", f"You're receiving this because you signed up for {_BRAND} updates."]
    return "\n".join(lines)


def _html_item(item) -> str:
    tag = ""
    if item.matched_on:
        chips = " ".join(
            f'<span style="background:#eef2ff;color:#3730a3;border-radius:10px;'
            f'padding:1px 8px;font-size:12px;margin-right:4px;">{escape(m)}</span>'
            for m in item.matched_on
        )
        tag = f'<div style="margin:4px 0 6px;">{chips}</div>'
    return (
        '<div style="margin:0 0 18px;padding:0 0 14px;border-bottom:1px solid #eee;">'
        f'<a href="{escape(item.url)}" style="font-size:16px;font-weight:600;'
        f'color:#111;text-decoration:none;">{escape(item.title)}</a>'
        f'<div style="font-size:12px;color:#888;margin:2px 0;">{escape(item.source)}</div>'
        f"{tag}"
        f'<div style="font-size:14px;color:#333;line-height:1.5;">{escape(item.summary)}</div>'
        "</div>"
    )


def _html_section(section) -> str:
    items = "".join(_html_item(i) for i in section.items)
    return (
        f'<h2 style="font-size:15px;text-transform:uppercase;letter-spacing:.04em;'
        f'color:#6b7280;margin:26px 0 12px;">{escape(section.heading)}</h2>{items}'
    )


def render_html(digest: Digest) -> str:
    empty = (
        '<p style="font-size:15px;color:#374151;">'
        "No new updates matched your preferences today.</p>"
    )
    body = empty if digest.is_empty else "".join(_html_section(s) for s in digest.sections)
    period = escape(digest.period)
    name = escape(digest.subscriber_name)
    footer = f"You're receiving this because you signed up for {_BRAND} updates."
    return (
        '<div style="max-width:640px;margin:0 auto;font-family:-apple-system,Segoe UI,Roboto,'
        'Helvetica,Arial,sans-serif;color:#111;padding:24px;">'
        f'<div style="font-size:22px;font-weight:700;">{_BRAND}</div>'
        f'<div style="font-size:13px;color:#9ca3af;margin-bottom:18px;">{period} digest</div>'
        f'<p style="font-size:16px;">Hi {name},</p>'
        f"{body}"
        '<div style="margin-top:28px;font-size:12px;color:#9ca3af;border-top:1px solid #eee;'
        f'padding-top:12px;">{footer}</div>'
        "</div>"
    )


def render_email(digest: Digest) -> RenderedEmail:
    return RenderedEmail(
        subject=_subject(digest), html=render_html(digest), text=render_text(digest)
    )
