# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""Decision-time ("as-of") context shared by the harness, tool servers and prompts.

The harness sets the as-of per task (from task metadata ``as_of``) in a ContextVar.
ToolManager passes it to every MCP subprocess through the ``AS_OF`` env var, so the
as-of is never a tool argument: the agent cannot move its own clock. As-of tool
servers fail closed when ``AS_OF`` is missing.

Convention: a date-only as-of ("2024-03-08") means the START of that day in
``AS_OF_DEFAULT_TZ`` (America/New_York by default) -- the decision is made before
anything published on that day. A naive datetime is read in the same zone.
"""

import datetime
import hashlib
import os
from contextlib import contextmanager
from contextvars import ContextVar
from zoneinfo import ZoneInfo

AS_OF_ENV = "AS_OF"
AS_OF_DEFAULT_TZ = os.environ.get("AS_OF_DEFAULT_TZ", "America/New_York")

AS_OF_CONTEXT_VAR: ContextVar[datetime.datetime | None] = ContextVar(
    "CURRENT_AS_OF", default=None
)


def parse_as_of(value: str) -> datetime.datetime:
    """Parse an as-of string into a timezone-aware UTC datetime."""
    value = value.strip()
    if len(value) == 10:  # YYYY-MM-DD
        day = datetime.date.fromisoformat(value)
        dt = datetime.datetime.combine(
            day, datetime.time(0, 0), tzinfo=ZoneInfo(AS_OF_DEFAULT_TZ)
        )
    else:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(AS_OF_DEFAULT_TZ))
    return dt.astimezone(datetime.timezone.utc)


def format_as_of(dt: datetime.datetime) -> str:
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def as_of_context(value: str | None):
    """Bind the as-of for the current task; a no-op when ``value`` is empty."""
    if not value:
        yield None
        return
    token = AS_OF_CONTEXT_VAR.set(parse_as_of(str(value)))
    try:
        yield AS_OF_CONTEXT_VAR.get()
    finally:
        AS_OF_CONTEXT_VAR.reset(token)


def current_as_of() -> datetime.datetime | None:
    """The as-of in effect: the task ContextVar in the harness, ``AS_OF`` env in a tool server."""
    dt = AS_OF_CONTEXT_VAR.get()
    if dt is not None:
        return dt
    raw = os.environ.get(AS_OF_ENV, "").strip()
    return parse_as_of(raw) if raw else None


def decision_date_str() -> str:
    """Date shown to the agent as "today": the as-of date when set, else the real date.

    Telling the agent the real date in a backtest is itself a leak.
    """
    dt = current_as_of()
    if dt is None:
        return datetime.datetime.today().strftime("%Y-%m-%d")
    return dt.astimezone(ZoneInfo(AS_OF_DEFAULT_TZ)).strftime("%Y-%m-%d")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def with_provenance(content: str, **fields) -> str:
    """Prefix tool output with a provenance block the agent and the auditor can both read."""
    lines = ["[AS-OF PROVENANCE]"]
    for key, value in fields.items():
        if value is None or value == "" or value == []:
            continue
        lines.append(f"{key}: {value}")
    lines.append(f"content_sha256: {sha256_text(content)}")
    lines.append("[END PROVENANCE]")
    return "\n".join(lines) + "\n\n" + content
