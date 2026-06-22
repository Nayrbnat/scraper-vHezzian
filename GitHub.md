# GitHub.md — ScrapeForge collaboration & agent-team workflow

> How we use git/GitHub, and how an **agent team** builds this repo in parallel without the usual merge
> hell. Companion docs: `CLAUDE.md` (shared standards), `SPEC.md` (contracts), `architecture.MD` (map),
> `planning.MD` (roadmap). **TL;DR of the findings:** keep worktrees, but make shared "seam" files
> conflict-free by construction, integrate continuously, and enforce one-feature-per-folder.

---

## 1. Repo facts & cold-start

- **Remote:** `origin → https://github.com/Nayrbnat/scraper-vHezzian.git`
- **State at writing:** no commits yet (`origin/main` gone). The repo is greenfield — the perfect time to
  set the workflow before agents fan out.

**Cold-start runbook (lead, once):**
```bash
git add .gitignore CLAUDE.md SPEC.md architecture.MD planning.MD GitHub.md
git commit -m "chore: scaffold docs, standards, and collaboration workflow"
git branch -M main
git push -u origin main
```
Then on GitHub, protect `main`:
- Require a PR before merging; require status checks (CI) to pass; require ≥1 review.
- Disallow force-pushes and direct pushes to `main`.
- (Optional) Add a `CODEOWNERS` file so PRs touching a feature's folder need that owner's review and
  PRs touching shared seam files need the **lead's** review.

Never commit secrets — `.gitignore` already excludes `.env`, `.states/`, `proxies.txt`, `output/`,
`*.enc`, `*.jsonl`/`*.manifest`.

---

## 2. Branching model (recommendation: trunk-based)

- **Default: short-lived feature branches → `main`** behind CI + branch protection. Small, frequent
  merges beat big-bang integration (this is the DORA finding and the direct fix for your conflict pain).
- **Naming:** `feat/<area>-<slug>`, `fix/<slug>`, `chore/<slug>`, `test/<slug>`, `docs/<slug>`.
  Examples: `feat/bucket3-public-scraper`, `feat/core-rate-limiter`, `fix/curl-impersonate-version`.
- **Lifetime:** hours-to-days, not weeks. Rebase on `main` **daily**. Squash-merge. Delete branch on
  merge.
- **Optional `develop` integration branch:** add one *only* when more than ~4 features fan out in
  parallel and you want conflicts surfaced in `develop` before they reach a release-stable `main`.
  Below that threshold, `develop` is just ceremony.

---

## 3. Worktree-per-agent — keep it, with discipline (the findings)

**Verdict: your worktree model is correct; the conflicts were never the worktrees' fault.** "Separate
folders" still forced every agent to edit the same handful of shared files — *those* are the conflict
hotspots:

| Shared "seam" file | Why every feature touched it | Fix (see §4) |
|---|---|---|
| `core/engine.py` `DOMAIN_REGISTRY` | every scraper registered its domain | `@register_scraper` decorator — no central edit |
| `config/settings.py` | every feature added config | per-module `Settings` fragments |
| `cli.py` | every feature added a command | per-bucket Typer sub-apps |
| `exceptions.py` / `__init__.py` | shared hierarchy / exports | subclass in-module; thin exports |
| `pyproject.toml` | every feature added a dep | declare up front; deps via the lead |

**Worktrees are good** — they give each agent a real isolated checkout (no clobbering another agent's
uncommitted work). The clutter/tech-debt came from missing lifecycle discipline. Rules:
- **One branch per worktree, one feature per worktree.** Never run two agents in one worktree.
- **Naming:** worktree dir `wt-<feature>` on branch `feat/<feature>` (kept out of the main tree, e.g.
  `../wt-bucket3-public`).
- **Rebase on `main` daily** inside the worktree so divergence stays small.
- **Prune on merge:** once the PR merges, remove the worktree and delete the branch — don't let stale
  worktrees pile up.

```bash
git worktree add ../wt-bucket3-public -b feat/bucket3-public-scraper
# ... agent works, commits, opens PR, PR merges ...
git worktree remove ../wt-bucket3-public
git branch -d feat/bucket3-public-scraper
git worktree prune
```

**Claude Code tooling that maps to this:** `EnterWorktree`/`ExitWorktree`, the Agent tool's
`isolation: "worktree"` option (auto-removed if unchanged), the `superpowers:using-git-worktrees` skill,
and `creating-agent-teams` for spinning up the roles.

---

## 4. Conflict-avoidance: agents only ADD files

The core rule (encoded as `SPEC.md` Invariant #16 and in `CLAUDE.md`): **adding a feature = adding
files, never editing shared seam files.**
- Scrapers self-register: `@register_scraper('site.com')` from `core/registry.py`. One file per scraper
  under `scrapers/{premium,community,public}/`. Two agents adding two sites edit two different files →
  **zero conflict**.
- CLI: per-feature Typer **sub-app** in your package, auto-mounted; root `cli.py` untouched.
- Config: per-module `BaseSettings` fragment in your module; core `Settings` untouched.
- Exceptions: subclass the base hierarchy inside your module.
- **Residual shared file — `pyproject.toml`:** the one file features may need in common. Mitigate by
  declaring the full known dependency set during the Foundation phase, grouping per-bucket extras under
  `[project.optional-dependencies]`, and routing any later addition **through the lead** (a tiny,
  sorted, low-conflict append). Log anything dropped/changed.

This removes ~80% of the historical conflict surface. What remains (rare logic merges) is small enough
for the lead to resolve quickly.

---

## 5. Agent-team roles & the Research → Plan → Build → Review loop

Roles live in `.claude/agents/` with **scoped tools** and **different models** (different blind spots).
State passes between agents through **files**, not one shared context window — this keeps each agent's
context clean and sidesteps premature compaction (§10).

| Role | Model | Tools | Reads | Writes |
|---|---|---|---|---|
| **planner** | opus | Read/Grep/Glob/Write/WebFetch/WebSearch | SPEC, architecture, planning | `PLAN.md` (the handoff) |
| **researcher** | sonnet | Read/Grep/Glob/WebFetch/WebSearch | live targets | findings (into PLAN) |
| **builder** | sonnet | Read/Edit/Write/Bash/Grep/Glob | `PLAN.md` + SPEC | code + tests in its folder |
| **reviewer** (adversarial) | **opus** | Read/Grep/Glob | the **diff only** | findings |
| **security-auditor** | opus | Read/Grep/Glob | the diff | pass/fail findings |
| **lead** (you / orchestrator) | — | full | everything | merges, conflict resolution |

The loop (reviewers come **last**, each in its own pass and context window):

```
plan (PLAN.md) → build (feature branch) → test → review (diff-only) → security → lead integrates → main
                                                   ▲ on the PR: the `reviewer` agent AND CodeRabbit
                                                     both read the diff independently (§8)
```

Why it's built this way (and the failure modes it avoids):
- **File-based handoff** (`PLAN.md`): the planner writes it, the builder executes it, the reviewer checks
  output *against* it. No stuffing everything into one window.
- **Adversarial reviewer sees only the diff** — not the builder's reasoning or the original task framing.
  That forces genuinely independent evaluation. LLMs cannot reliably self-correct from their own
  reasoning (they flip correct→incorrect about as often as they fix things), so we use a **different
  model** and withhold the builder's rationale.
- **Reviewer escalates only on substantive grounds** — forced disagreement degrades quality. No
  manufactured nitpicks.
- **Tool scoping is the guardrail:** reviewers/auditors/researchers are Read/Grep/Glob (+WebFetch for
  research) — they physically cannot "helpfully" rewrite the code they're supposed to critique. Only
  builders hold Write/Edit/Bash.

---

## 6. Commit & PR conventions

- **Conventional Commits:** `feat:`, `fix:`, `chore:`, `test:`, `docs:`, `refactor:`. Imperative mood,
  small and focused.
- **Footer on every commit:**
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **PRs:** small (one feature), opened via `gh`. Draft PRs for WIP. Body should state what + why, the
  SPEC sections touched, test evidence, and end with:
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
- Use `gh pr create`, `gh pr view`, `gh pr checks`; never push straight to `main`.

### 6.1 Auto-commit cadence (standing policy)

Agents **commit automatically after each completed unit of work** — a finished module, a passing test
file, a doc update — without waiting to be asked. Each commit is small, Conventional-Commit-titled, and
carries the `Co-Authored-By` footer. Don't batch a day's work into one giant commit; commit at natural
checkpoints so history is reviewable and `/compact`-safe.

**Push target is always a branch, never `main`:**

```
work → auto-commit (small) → push to feat/<slug> → open/refresh PR → review → squash-merge to main
```

- **`main` is never pushed to directly.** It advances **only** through merged PRs (a server-side merge,
  not a `git push origin main`). Branch protection enforces this.
- Push the feature branch early and often (`git push -u origin feat/<slug>`); the PR updates itself as
  you add commits. Use a **draft PR** while WIP, mark ready when the Definition of Done (§7) is met.
- The repository's very first `main` is an **empty initialization commit** (the PR base). Everything of
  substance — including the initial scaffold — reaches `main` through its PR, so no real work is ever
  pushed to `main` directly.

---

## 7. Merge / integration protocol

- **Rebase before merge** (`git rebase origin/main`), resolve locally, re-run CI.
- **Squash-merge** to keep `main` history linear and readable; delete the branch.
- **Who resolves conflicts:** the **lead**. Feature builders rebase their own small branches; cross-cutting
  conflicts (rare, mostly `pyproject.toml`) are the lead's call.
- **Definition of Done (gate to merge):** CI green (`ruff` + `pytest -m "not integration"`) **and**
  reviewer approved **and** (for anything touching auth/state/proxies) security-auditor pass.
- **Integrate continuously**, not at the end. A feature that's been isolated for a week is the source of
  big-bang pain — merge in slices.

---

## 8. CI/CD (GitHub Actions) + CodeRabbit

The real workflow lives at **`.github/workflows/ci.yml`** (not a sketch). On every PR + push to `main`,
across Python 3.11/3.12:
- `ruff check .` → `ruff format --check .`
- `pytest -m "not integration" --cov --cov-fail-under=80` (a **pgvector** service container backs `@db`
  tests; integration tests need proxies/creds → never in CI)
- `gitleaks` secret scan

Make all jobs **required status checks** in branch protection; integration tests stay manual/local
(`pytest -m integration`). Config for the suite lives in `pyproject.toml`; strategy + the per-module test
matrix live in `TESTING.md`.

**CodeRabbit** (`.coderabbit.yaml`) runs as a **second, AI reviewer** on every non-draft PR — it enforces
our conventions (SPEC invariants, the seam rules, "API never drives a browser") and may *suggest* tests.
It is a reviewer, **not** a test runner, and **augments** (never replaces) the adversarial `reviewer`
agent (§5) and the pytest suite.

---

## 9. Secrets & safety

- **Never commit:** `.env`, cookies, encrypted state (`.states/`, `*.enc`), `proxies.txt`, scraped
  `output/`. All are git-ignored; the security-auditor treats a leak as an automatic blocker.
- **Private repo** posture for a scraping project; enable GitHub secret scanning + push protection.
- **Encrypted state stays out of git** entirely — it's machine-local under `~/.scrapeforge/states/`.
- Rotate the Fernet `STATE_STORE_KEY` out-of-band; it lives only in `.env`.

---

## 10. Compaction-aware fan-out (so you stop losing work)

Returning many large subagent outputs into one window forces early auto-compaction and the "chat reached
its limit" failure. Mechanics (as of early 2026): the window reserves a buffer (~33K tokens) you can't
use, and auto-compaction triggers around ~83.5% of the window. Implications:
- **Prefer file handoff** (`PLAN.md`, research files, findings files) over returning big blobs into the
  lead's context.
- **For wide fan-out, use a `Workflow`** — state lives in the script, not the chat window, so dozens of
  agents don't collapse one context.
- Keep the `CLAUDE.md` **/compact policy** in force so that if compaction does fire, the API changes +
  rationale, error→solution pairs, modified-file list, and PLAN status survive.

---

## 11. Never-do list

- ❌ Force-push `main` / rewrite shared history.
- ❌ Skip hooks (`--no-verify`) or bypass signing unless explicitly authorized.
- ❌ Long-lived feature branches / big-bang end-of-project merges.
- ❌ Edit shared seam files to add a feature (use the registry / sub-app / settings-fragment seams).
- ❌ Edit another feature's folder (Invariant #17).
- ❌ Let a reviewer/auditor edit code (they report; builders fix).
- ❌ Commit secrets, cookies, proxies, or scraped output.
- ❌ Leave stale worktrees/branches after merge — prune them.
