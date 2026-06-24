"""Runtime imports must be declared as runtime dependencies, not test extras.

Regression guard for the first live deploy failure: ``ModuleNotFoundError: No module named
'httpx'`` during ``pipeline summarize`` — httpx was only in the ``test`` optional-deps, but the
OpenAI-compatible summarizer imports it at runtime, so the runtime-only ``pip install .`` on the
GitHub Actions runner could not summarize.
"""

from __future__ import annotations

import tomllib
from pathlib import Path


def _runtime_dep_names() -> set[str]:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    names = set()
    for spec in data["project"]["dependencies"]:
        # "httpx>=0.27" -> "httpx"; "sqlalchemy[asyncio]>=2.0" -> "sqlalchemy"
        name = spec.replace(" ", "")
        for sep in (">", "<", "=", "[", ";", "!", "~"):
            name = name.split(sep)[0]
        names.add(name.lower())
    return names


def test_httpx_is_a_runtime_dependency() -> None:
    assert "httpx" in _runtime_dep_names(), (
        "httpx must be in [project.dependencies]: the summarizer imports it at runtime"
    )
