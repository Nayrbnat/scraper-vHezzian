# Curated investing Substacks — company & sector deep dives (50)

<!-- Compiled + LIVE-VERIFIED 2026-06-22 against the public Substack archive API -->
<!-- (`GET https://<host>/api/v1/archive?sort=new&limit=N`). Canonical list lives in code: -->
<!-- scrapeforge/scrapers/community/substack_sources.py — this doc is the human-readable companion. -->

> The brief: "SemiAnalysis, but for **all** sectors." Fifty Substack publications that do
> rigorous, fundamentals-driven **deep dives into companies or sectors** — spanning semis,
> software, value/special-situations, financials & fintech, energy & industrials, biotech &
> healthcare, single-sector specialists (gaming, defense, crypto, cleantech/EV, CPG), and
> China/global + big-tech context.

## How this was built (evidence before assertions)

Every host below was hit live (2026-06-22) on the same public endpoint the scraper uses
(`/api/v1/archive`). A candidate was **kept only if** it returned HTTP 200 with a non-empty
JSON array **and** had a recent post (active publication). Live verification removed a dozen
plausible-but-dead entries — placeholder "Coming soon" pages (The Diff's `*.substack.com`
shell, Speedwell Research, The Energy Realist, Future of Mobility, Retail Adventures), a
"Test Post" stub (Defense Investing), a "has moved" tombstone (The DeFi Report), and
publications inactive since 2022–2023 (The Biopharma Report, Punch Card Investor). Each was
swapped for a verified-active alternative. Custom domains that 301-redirect from their bare
subdomain were followed to the canonical host, which is what is stored.

The `paid-leaning` tag means the **newest** post sat behind a paid tier at verification time.
It is an informational hint only — **not** a gate. The scraper filters public vs. paid
per-post at fetch time (SPEC Invariant #15), so a paid-leaning publication still yields its
free posts.

## The list (grouped by sector)

### Semiconductors & hardware — the SemiAnalysis lane (5)
| Publication | Host | |
|---|---|---|
| SemiAnalysis | `newsletter.semianalysis.com` | paid-leaning |
| Fabricated Knowledge | `www.fabricatedknowledge.com` | paid-leaning |
| Chipstrat | `www.chipstrat.com` | |
| The Chip Letter | `thechipletter.substack.com` | |
| Asianometry | `www.asianometry.com` | |

### Software, internet & tech equities (6)
| Publication | Host | |
|---|---|---|
| App Economy Insights | `www.appeconomyinsights.com` | |
| Clouded Judgement (Jamin Ball) | `cloudedjudgement.substack.com` | |
| The Wolf of Harcourt Street | `www.thewolfofharcourtstreet.com` | |
| Rijnberk InvestInsights | `rijnberkinvestinsights.substack.com` | |
| TechFund | `www.techinvestments.io` | paid-leaning |
| Not Boring (Packy McCormick) | `www.notboring.co` | paid-leaning |

### Fundamental / value equity research (13)
| Publication | Host | |
|---|---|---|
| MBI Deep Dives | `mbideepdives.substack.com` | paid-leaning |
| StockOpine | `www.stockopine.com` | |
| TSOH Investment Research | `thescienceofhitting.com` | paid-leaning |
| Best Anchor Stocks | `www.bestanchorstocks.com` | paid-leaning |
| Yet Another Value Blog | `www.yetanothervalueblog.com` | |
| Kingswell | `www.kingswell.io` | |
| The Intrinsic Investor | `theintrinsicinvestor.substack.com` | |
| Invariant | `invariant.substack.com` | |
| Clark Square Capital | `www.clarksquarecapital.com` | paid-leaning |
| Eagle Point Capital | `eaglepointcapital.substack.com` | |
| Special Situation Investing | `specialsituationinvesting.substack.com` | |
| Investment Talk | `www.investmenttalk.co` | |
| The Finance Corner | `thefinancecorner.substack.com` | |

### Growth & thematic (3)
| Publication | Host | |
|---|---|---|
| Citrini Research | `www.citriniresearch.com` | paid-leaning |
| Growth Stock Deep Dives | `growthstockdeepdives.substack.com` | |
| The Generalist | `www.generalist.com` | paid-leaning |

### Forensic, short & governance (3)
| Publication | Host | |
|---|---|---|
| The Bear Cave (Edwin Dorsey) | `thebearcave.substack.com` | |
| NonGAAP Investing | `www.nongaap.com` | paid-leaning |
| Security Analysis | `www.securityanalysis.org` | paid-leaning |

### Financials & fintech (3)
| Publication | Host | |
|---|---|---|
| Net Interest (Marc Rubinstein) | `www.netinterest.co` | paid-leaning |
| Fintech Business Weekly (Jason Mikula) | `fintechbusinessweekly.substack.com` | paid-leaning |
| The Fintech Blueprint | `thefintechblueprint.substack.com` | |

### Energy, commodities & industrials (5)
| Publication | Host | |
|---|---|---|
| Doomberg | `newsletter.doomberg.com` | paid-leaning |
| HFI Research | `www.hfir.com` | paid-leaning |
| Open Insights | `www.openinsightscap.com` | |
| Super-Spiked (Arjun Murti) | `arjunmurti.substack.com` | |
| Construction Physics (Brian Potter) | `www.construction-physics.com` | paid-leaning |

### Biotech & healthcare (5)
| Publication | Host | |
|---|---|---|
| BowTiedBiotech | `bowtiedbiotech.substack.com` | paid-leaning |
| Matt Gamber's Biotech | `mattbiotech.substack.com` | paid-leaning |
| Biotech Blueprint | `www.biotechblueprint.com` | |
| Biotech Analysis: 0 to 1 | `adus.substack.com` | |
| Hartaj Singh (pharma) | `hartajsingh1.substack.com` | |

### Single-sector specialists (5)
| Publication | Host | Sector | |
|---|---|---|---|
| Naavik | `naavik.substack.com` | Games industry | |
| The Merge | `themerge.substack.com` | Defense / aerospace | |
| DeFi Education | `defieducation.substack.com` | Crypto / DeFi | paid-leaning |
| CleanTechnica | `cleantechnica.substack.com` | Cleantech / EVs | paid-leaning |
| Snaxshot | `www.snaxshot.com` | Consumer / CPG | |

### China & big-tech context (2)
| Publication | Host | |
|---|---|---|
| Baiguan | `www.baiguan.news` | paid-leaning |
| Big Technology (Alex Kantrowitz) | `www.bigtechnology.com` | |

## How it's wired in

The canonical list is `SUBSTACK_INVESTING_SOURCES` in
`scrapeforge/scrapers/community/substack_sources.py`. It is **extension by addition** —
one new file in the community package; nothing in `engine.py` or a central registry is
touched (Invariant #16). The `SubstackScraper` already self-registers for `*.substack.com`
and custom domains, so the list only names the publications and offers selection helpers
(`select_sources`, `by_sector`, `sectors`).

The working integration is the community CLI, which drives
`SubstackScraper.scrape_publication` (offset-paginated archive discovery + per-post fetch,
public-only) and writes every article to a JSONL sink:

```bash
# List what would be scraped (no network, no writes):
scrapeforge community scrape-substacks --list
scrapeforge community scrape-substacks --sector "Biotech & Healthcare" --list

# Scrape a sector (3 publications, 2 posts each) to ./output.jsonl:
scrapeforge community scrape-substacks --sector Semiconductors --max 3 --limit 2

# Scrape the whole curated list:
scrapeforge community scrape-substacks --limit 10 --output ./substacks

# Or one publication on its own:
scrapeforge community scrape substack www.chipstrat.com --limit 5
```

The resulting JSONL (`Article` records, `metadata.bucket="community"`) feeds the digest via
its `--source` flag. Verified live (2026-06-22): `scrape-substacks --sector Semiconductors
--max 3 --limit 2` returned 6 real, parsed articles (SemiAnalysis / Fabricated Knowledge /
Chipstrat) with titles, bylines and tz-aware dates.

### Not yet: recurring scheduler ingestion

These are *publication* hosts, not single-post URLs. The Phase-6 scraper worker
(`handle_scrape_job` → `engine.scrape(url)`) is **single-URL**; it has no
publication-discovery/fan-out step (publication → N post URLs → N jobs). So the curated list
is deliberately **not** seeded into the `sources` table — enqueuing a bare host would feed the
single-post path a slug-less URL. Recurring, scheduler-driven ingestion of whole publications
is a follow-up that adds a fan-out worker; the curated list and its helpers are ready to plug
into it without change.

## Re-verification

Hosts drift — publications migrate domains, go paid-only, or go dormant. Re-run the archive
probe periodically and swap any that stop returning a recent, non-empty array. The selection
rule that built this list: **HTTP 200 + non-empty JSON array + a recent post.**
