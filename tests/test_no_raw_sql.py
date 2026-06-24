"""Guard: no dynamically-built SQL strings in the codebase (SQL-injection safety).

All DB access must go through SQLAlchemy ORM/Core with bound parameters. The only allowed raw
SQL is STATIC DDL/maintenance via ``text("…")`` with NO f-string / % / .format interpolation.
"""

from __future__ import annotations

import re
from pathlib import Path

# Flags f-string or %/.format interpolation inside a text("...") / execute("...") call.
_DYNAMIC_SQL = re.compile(r"""(text|execute)\(\s*f["']|(text|execute)\([^)]*%[^)]*\)""")


def test_no_dynamic_sql_strings() -> None:
    offenders = []
    for path in Path("scrapeforge").rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for m in _DYNAMIC_SQL.finditer(src):
            line = src[: m.start()].count("\n") + 1
            offenders.append(f"{path}:{line}")
    assert not offenders, "Dynamic SQL string(s) found (use bound params): " + ", ".join(offenders)
