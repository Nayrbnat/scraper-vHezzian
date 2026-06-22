# CLAUDE.md — ScrapeForge shared standards

> Every teammate (every agent) loads this from its working directory. It is the **operating manual**:
> shared standards, gates, and the rules that keep parallel work conflict-free. It is **not** the
> object-model spec — that is `SPEC.md` (formerly `claude.md`; renamed to avoid the `CLAUDE.md`
> case-collision on Windows).

## 0. Orientation — read before coding
- `SPEC.md` — class contracts, object graph, **Invariants #1–#17** (the document wins over code).
- `architecture.MD` — directory tree, module map, data flows, how to add/remove a module.
- `planning.MD` — phased roadmap and acceptance criteria.
- `GitHub.md` — branching, worktrees, PR/commit conventions, CI, the agent-team workflow.
- **Your task/handoff file (`PLAN.md` or the prompt you were given) — read it first.** Build to the
  plan; if the plan and the code disagree, raise it, don't silently diverge.

## 1. Engineering standards
- **SRP / SLAP.** One responsibility per module; one level of abstraction per function. A file that
  keeps growing is a smell — split it.
- **Async-first.** All driver/bridge/scraper/engine I/O is `async`. No `sync_api`, no `asyncio.run()`
  inside the loop, no blocking I/O on the event loop (wrap sync libs in `asyncio.to_thread`).
  See `SPEC.md` Invariants #11–#15.
- **Typed exceptions only** (`ScrapeForgeError` hierarchy). No bare `except:`; no returning `None` to
  signal failure where an exception belongs.
- **No secrets in code or git.** Credentials/cookies/proxies live in `.env` / encrypted state — never
  committed (see `.gitignore`).
- **Match the surrounding code** — naming, comment density, idioms.

## 2. Seam rules — extension by ADDITION (this is what prevents merge conflicts)
Adding a feature means **adding files**, never editing shared "seam" files. Concretely:
- **Scrapers self-register:** `@register_scraper('site.com')` from `core/registry.py`. Do **not** edit
  `core/engine.py` or any central registry dict (Invariant #16).
- **One file per scraper:** `scrapers/{premium,community,public}/<name>.py`. Two agents adding two sites
  touch two different files.
- **CLI:** add a per-feature Typer **sub-app** in your package; don't edit the root `cli.py`.
- **Config:** add a per-module `BaseSettings` fragment in your module; don't append to the core
  `Settings` class.
- **Exceptions:** subclass the base hierarchy inside your module; don't edit `exceptions.py`.
- **The one sanctioned shared file is `pyproject.toml`** (dependencies). Declare deps up front; any new
  dependency goes **through the lead**, not added unilaterally on a feature branch.
- **Stay in your lane:** never edit another feature's files (Invariant #17; enforced by CODEOWNERS).

## 3. Test & lint gates — Definition of Done
A change is "done" only when all of these pass and a reviewer has approved:
```bash
ruff check .            # 0 errors
ruff format --check .   # formatted
pytest -m "not integration" --cov --cov-fail-under=80   # unit/contract green + coverage gate (CI enforces)
pytest -m "not integration" path/to/test_x.py::test_y   # focused run while iterating
```
- **Write the test first** (TDD) for any feature or bugfix. Test config is in `pyproject.toml`; the
  per-module test matrix is in `TESTING.md`. CI (`.github/workflows/ci.yml`) runs the same gates.
- Integration tests (`@pytest.mark.integration`) hit live sites — run manually, never in CI.
- Don't claim success without running the command and seeing it pass. Evidence before assertions.

## 4. /compact policy (must survive auto-compaction)
When this conversation is auto-compacted, **preserve**:
1. All interface/API/contract changes **and their rationale**.
2. Error messages encountered **and the solution** that fixed each.
3. The running **list of modified/created files**.
4. Current **branch / feature / worktree** and the **PLAN.md task status** (done / in-progress / blocked).
5. Any deviation from `SPEC.md` and why.
Drop: exploratory dead-ends, raw file dumps already saved to disk, redundant tool output.

## 5. Teammate etiquette (multi-agent)
- **Handoff via files, not chat:** planner writes `PLAN.md`; builders read it; the reviewer checks the
  diff against it. Keep each context window clean.
- **One branch + one worktree per feature.** Branch naming and worktree lifecycle: see `GitHub.md`.
- **Commits:** Conventional Commits (`feat:`, `fix:`, `chore:`, `test:`, `docs:`). End every commit with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Rebase on `main` daily**; open small PRs; never force-push `main`; never skip hooks.
- **Reviewers don't edit code** — they report findings; the builder applies them.
