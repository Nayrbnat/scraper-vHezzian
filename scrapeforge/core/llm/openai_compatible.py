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
    """Coerce *value* to an int in [1, 10]; raise LLMParseError if non-numeric."""
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
) -> list[dict]:
    today = datetime.now(UTC).date().isoformat()
    pub = published.date().isoformat() if published else "unknown"
    port = ", ".join(portfolio) or "(none given)"
    inter = ", ".join(interests) or "(none given)"
    system = (
        "You are an equity analyst scoring and summarizing articles for a specific investor. "
        f"The investor's portfolio: {port}. The investor's stated interests (including niche "
        f"topics): {inter}. Today's date is {today}; the article was published {pub}. "
        "Produce: (a) exactly 5 short bullets (<=25 words each) capturing the core claim/thesis, "
        "the company/ticker or sector, the key number or catalyst, the bull/bear angle, and why "
        "it matters; (b) integer 1-10 sub-scores for relevance (about AI, finance, the portfolio, "
        "or secular industry shifts), credibility (famous/respected Substack or leading "
        "researcher), intensity (fundraising / new investment / collaboration / technological "
        "breakthrough rank high), personal (matches the investor's stated interests, niche ones "
        "count), time (imminent/time-sensitive events rank high); (c) an overall relevance 1-10 "
        "weighing those for THIS investor; (d) a one-line reason. Return ONLY a JSON object: "
        '{"bullets":[5 strings],"scores":{"relevance":n,"credibility":n,"intensity":n,'
        '"personal":n,"time":n},"relevance":n,"reason":"..."}.'
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
        last_status = None
        async with httpx.AsyncClient(timeout=self._s.SUMMARY_REQUEST_TIMEOUT) as client:
            for attempt in range(self._s.SUMMARY_MAX_RETRIES + 1):
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except httpx.TimeoutException:
                    last_status = "timeout"
                else:
                    if resp.status_code == 429:
                        last_status = 429
                    elif resp.status_code >= 400:
                        raise LLMError(f"LLM HTTP {resp.status_code}")
                    else:
                        return resp.json()["choices"][0]["message"]["content"]
                if attempt < self._s.SUMMARY_MAX_RETRIES:
                    await asyncio.sleep(0.5 * (attempt + 1))
        raise LLMRateLimitError(f"rate-limited/timeout after retries (last={last_status})")

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
