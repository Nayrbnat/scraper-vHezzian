"""OpenAI-compatible chat-completions adapter (GLM/DeepSeek/Qwen) for the Summarizer port."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime

import httpx

from scrapeforge.core.llm.base import Summarizer, SummaryResult
from scrapeforge.core.llm.exceptions import LLMError, LLMParseError, LLMRateLimitError
from scrapeforge.core.llm.settings import SummarizerSettings

log = logging.getLogger(__name__)

_SCORE_KEYS = ("relevance", "credibility", "intensity", "personal", "time")


def _clamp_1_10(value: object) -> int:
    """Coerce *value* to an int in [1, 10]; raise LLMParseError if non-numeric or bool."""
    if isinstance(value, bool):
        raise LLMParseError(f"non-numeric score: {value!r}")
    try:
        n = int(round(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise LLMParseError(f"non-numeric score: {value!r}") from exc
    return max(1, min(10, n))


def _loads(text: str) -> dict:
    """Parse a JSON object from *text*; fall back to the first {...} block."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match is None:
            raise LLMParseError("no JSON object in model response") from None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise LLMParseError(f"unparseable JSON: {exc}") from exc


def _build_messages(
    *,
    title: str,
    content: str,
    published: datetime | None,
    portfolio: list[str],
    interests: list[str],
    max_chars: int,
    focus: str = "artificial intelligence and finance",
) -> list[dict]:
    today = datetime.now(UTC).date().isoformat()
    pub = published.date().isoformat() if published else "unknown"
    port = ", ".join(portfolio) or "(none given)"
    inter = ", ".join(interests) or "(none given)"
    system = (
        f"You are an equity analyst preparing a daily briefing for an investor focused on {focus}. "
        f"The investor's portfolio: {port}. Stated interests (incl. niche topics): {inter}. "
        f"Today is {today}; this article was published {pub}.\n\n"
        "Write EXACTLY 5 bullets. Rules for the bullets:\n"
        "- Each bullet is a DISTINCT, self-contained takeaway — no overlap between bullets, no "
        "filler, no generic phrasing, and do NOT just restate the headline.\n"
        "- Be concrete and specific: name the company/ticker/sector and cite the key number, "
        "date, or catalyst. Lead with the substance, not 'The article discusses...'.\n"
        "- <= 25 words each.\n"
        "Cover these five angles, in this order, one per bullet:\n"
        "  1. The single core claim or thesis.\n"
        "  2. The most important hard number, metric, or financial figure.\n"
        "  3. The specific catalyst, event, or change driving it.\n"
        "  4. The bull-vs-bear or key-risk angle.\n"
        "  5. The forward implication — what to watch next, or why it matters now.\n\n"
        f"Then score the article 1-10 on each: relevance (how directly it concerns {focus} — this "
        "is the PRIMARY axis; the investor's portfolio names and major secular industry shifts "
        "also count; an article unrelated to those areas scores low), credibility (respected "
        "publication/author), intensity (fundraising, M&A, launches, breakthroughs rank high), "
        "personal (matches the stated interests), time (how recent and time-sensitive — latest, "
        f"breaking developments rank high). Give an overall relevance 1-10 that PRIORITIZES recent "
        f"{focus} developments for THIS investor, then a one-line reason. "
        'Return ONLY a JSON object: {"bullets":[5 strings],"scores":{"relevance":n,'
        '"credibility":n,"intensity":n,"personal":n,"time":n},"relevance":n,"reason":"..."}.'
    )
    user = f"Title: {title}\n\n{content[:max_chars]}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


class OpenAICompatibleSummarizer(Summarizer):
    """Calls any OpenAI-compatible /chat/completions endpoint and parses the result."""

    def __init__(self, settings: SummarizerSettings) -> None:
        self._s = settings

    async def summarize(
        self,
        *,
        title: str,
        content: str,
        published: datetime | None,
        portfolio: list[str],
        interests: list[str],
    ) -> SummaryResult:
        messages = _build_messages(
            title=title,
            content=content,
            published=published,
            portfolio=portfolio,
            interests=interests,
            max_chars=self._s.SUMMARY_MAX_INPUT_CHARS,
            focus=self._s.SUMMARY_FOCUS,
        )
        payload = {
            "model": self._s.SUMMARY_MODEL,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
        }
        headers = {"Authorization": f"Bearer {self._s.SUMMARY_API_KEY}"}
        url = f"{self._s.SUMMARY_API_BASE_URL.rstrip('/')}/chat/completions"

        text = await self._post_with_retry(url, payload, headers)
        return self._parse(text)

    async def _post_with_retry(self, url: str, payload: dict, headers: dict) -> str:
        saw_429 = False
        async with httpx.AsyncClient(timeout=self._s.SUMMARY_REQUEST_TIMEOUT) as client:
            for attempt in range(self._s.SUMMARY_MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except httpx.TimeoutException:
                    pass  # transient (slow reasoning model); retry, then fall through to LLMError
                else:
                    if resp.status_code == 429:
                        saw_429 = True
                    elif resp.status_code >= 400:
                        raise LLMError(f"LLM HTTP {resp.status_code}")
                    else:
                        try:
                            data = resp.json()
                            return data["choices"][0]["message"]["content"]
                        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                            raise LLMParseError("malformed completion envelope") from exc
                if attempt < self._s.SUMMARY_MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
        # A real 429 stops the run (respect the rate limit). A pure timeout is a per-article
        # failure the worker SKIPS — a single slow reasoning response must not kill the whole batch.
        if saw_429:
            raise LLMRateLimitError("rate-limited after retries")
        raise LLMError("request timed out after retries")

    def _parse(self, text: str) -> SummaryResult:
        obj = _loads(text)
        raw = obj.get("bullets")
        if not isinstance(raw, list):
            raise LLMParseError("missing 'bullets' array")
        bullets = [b.strip() for b in raw if isinstance(b, str) and b.strip()][:5]
        if len(bullets) < 3:
            raise LLMParseError(f"only {len(bullets)} usable bullets (need >=3)")
        raw_scores = obj.get("scores")
        if not isinstance(raw_scores, dict):
            raise LLMParseError("missing 'scores' object")
        scores = {k: _clamp_1_10(raw_scores.get(k)) for k in _SCORE_KEYS}
        relevance = _clamp_1_10(obj.get("relevance"))
        reason = str(obj.get("reason") or "").strip()
        return SummaryResult(
            bullets=bullets,
            relevance=relevance,
            scores=scores,
            reason=reason,
            model=self._s.SUMMARY_MODEL,
        )
