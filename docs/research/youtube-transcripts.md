# Site Playbook — YouTube transcripts (Bucket 4, Phase 8)
<!-- Researched: 2026-06-22 · adopted stack chosen by the user -->

> Curated gameplan for a new bucket: track AI/finance YouTubers → detect new uploads → pull
> transcripts → clean text → embed into pgvector. **Not built from scratch** — adopt existing libs.
> **Status: SCHEDULED (Phase 8), blocked on the user's go-ahead to clone/vendor the chosen repos.**

## Adopted stack (user decision)
**Primary:** `youtube-transcript-api` (transcripts) + `scrapetube` (channel upload detection).
**Fallback:** `yt-dlp` (+ `faster-whisper` ASR) for videos where captions are blocked/disabled.

## Anti-block reality (2026) — the hard constraint
- **Datacenter IPs are reliably blocked** for transcript + InnerTube calls. **Residential proxies are
  mandatory** server-side (youtube-transcript-api has native Webshare/generic proxy config — wire it to
  the existing `ProxyRotator`). Never run live YouTube fetches in CI — integration-only.
- **`PoTokenRequired`**: YouTube's bot-check breaks a growing subset of videos on the transcript API
  (unresolved upstream). This is exactly why the **yt-dlp + faster-whisper fallback** exists.
- **YouTube RSS feeds** (`feeds/videos.xml?channel_id=...`) are unreliable in 2026 (24–48h gaps) — use as
  a supplemental ping only, not the sole detector.

## Shortlist (7 candidates — for the user to review before cloning)
| Repo | Role | Stars | License | Async | Proxy | 2026 caveat |
|---|---|---|---|---|---|---|
| **youtube-transcript-api** (jdepoix) | transcripts (PRIMARY) | 7.8k | MIT | wrap in thread | first-class (Webshare/generic) | PoTokenRequired on a growing subset → needs fallback |
| **scrapetube** (dermasmid) | channel upload detection (PRIMARY) | 517 | MIT | sync | undocumented | listing only, no transcripts |
| **yt-dlp** | subtitles + channel + ASR audio (FALLBACK) | 173k | Unlicense (pip) | sync | yes (`--proxy`) | needs a JS runtime (Deno/Node) in the image |
| **faster-whisper** (SYSTRAN) | ASR fallback (captions disabled/blocked) | 23.8k | MIT | wrap in thread | n/a | needs a GPU for practical speed |
| **tubescrape** (zaidkx37) | async transcripts + channel | 9 | MIT | native | built-in rotation | brand-new, unproven at scale |
| **pytubefix** (JuanBindez) | captions (secondary) | 1.5k | MIT | AsyncYouTube | unverified | transcript is a side feature |
| **ytfetcher** (kaya70875) | bulk backfill wrapper | 80 | MIT | unclear | inherits #1 | wraps #1, single-dev |

## Channel-upload detection options
| Method | Auth | 2026 reliability | Note |
|---|---|---|---|
| `scrapetube.get_channel()` (InnerTube) | none | good | primary; sync (wrap in thread); MIT |
| `yt-dlp --flat-playlist` on the channel | none | good | needs JS runtime; pairs with `--download-archive` for idempotent deltas |
| YouTube RSS `feeds/videos.xml` | none | degraded | supplemental ping only |
| Data API v3 `playlistItems.list` | API key | excellent | quota-limited; no transcripts |

## Suggested architecture (reuses the whole foundation — self-registers as a new bucket)
1. **Track** — poll each channel's uploads (`scrapetube`/`yt-dlp --flat-playlist`), dedup new video IDs vs
   a DB seen-set. (RSS as a lightweight supplemental trigger.)
2. **Transcript** — per new video, `youtube-transcript-api` via `asyncio.to_thread`, routed through the
   existing residential `ProxyRotator`. Catch `PoTokenRequired`/`IpBlocked` explicitly.
3. **ASR fallback** — on PoToken/captions-disabled: `yt-dlp` (audio) → `faster-whisper` (GPU-gated, queued).
4. **Clean** — strip SRT/VTT timestamps, collapse dupes → plain UTF-8 text + metadata (video id, channel,
   publish date, title).
5. **Sink** — `Article(content=transcript)` flows through the SAME pipeline (claim-check raw → transform →
   PostgresSink). Embeddings into pgvector come with the RAG phase.

Fits as `scrapers/youtube/` self-registering via `@register_scraper` (Invariant #16) — no engine edits.

## Open decisions for the user (before Phase 8 starts)
- Confirm which repos to clone/vendor (primary stack chosen; confirm fallback scope).
- Residential proxy provider (Webshare has native youtube-transcript-api support).
- Whether to provision a GPU for the Whisper fallback now or defer (queue ASR jobs until available).
- Channel list (the AI/finance YouTubers to track).

## Sources
youtube-transcript-api: https://github.com/jdepoix/youtube-transcript-api (PoToken #592) ·
scrapetube: https://github.com/dermasmid/scrapetube · yt-dlp: https://github.com/yt-dlp/yt-dlp (JS runtime #15012) ·
faster-whisper: https://github.com/SYSTRAN/faster-whisper · tubescrape: https://github.com/zaidkx37/tubescrape ·
pytubefix: https://github.com/JuanBindez/pytubefix · ytfetcher: https://github.com/kaya70875/ytfetcher
