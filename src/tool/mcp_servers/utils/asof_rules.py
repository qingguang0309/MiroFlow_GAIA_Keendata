# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""Pure point-in-time rules used by the as-of tool servers (no network, unit-tested)."""

import datetime
import re
from zoneinfo import ZoneInfo

# ALFRED vintages are dated by calendar day (US Eastern). A vintage dated D may have been
# published at any time during D, so it only counts as available once D is over.
FRED_TZ = ZoneInfo("America/New_York")


def fred_vintage_date(as_of: datetime.datetime) -> datetime.date:
    """Latest ALFRED real-time date whose whole day ended at or before ``as_of``."""
    return as_of.astimezone(FRED_TZ).date() - datetime.timedelta(days=1)


def filter_fred_observations(
    observations: list[dict], vintage: datetime.date
) -> tuple[list[dict], int]:
    """Defense in depth: drop rows the API should never have returned for this vintage."""
    kept, dropped = [], 0
    for obs in observations:
        try:
            rt_start = datetime.date.fromisoformat(obs["realtime_start"])
            obs_date = datetime.date.fromisoformat(obs["date"])
        except (KeyError, ValueError):
            dropped += 1
            continue
        if rt_start > vintage or obs_date > vintage:
            dropped += 1
            continue
        kept.append(obs)
    return kept, dropped


# Series metadata fields safe to show at a vintage. Fields describing today's state
# (last_updated, observation_end, notes, popularity) are dropped.
FRED_SERIES_FIELDS = (
    "id",
    "title",
    "frequency",
    "units",
    "seasonal_adjustment",
    "observation_start",
)


# ---------------------------------------------------------------- Wayback

_WAYBACK_RE = re.compile(
    r"^(?:https?://)?(?:web\.)?archive\.org/web/(\d{1,14})[a-z_]*/(.+)$", re.I
)


def wayback_timestamp(dt: datetime.datetime) -> str:
    """Wayback timestamps are 14-digit UTC."""
    return dt.astimezone(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")


def parse_wayback_timestamp(ts: str) -> datetime.datetime:
    ts = ts.ljust(14, "0")
    return datetime.datetime.strptime(ts, "%Y%m%d%H%M%S").replace(
        tzinfo=datetime.timezone.utc
    )


def normalize_target_url(url: str) -> str | None:
    """Return the original URL the agent wants, or None if it is not fetchable.

    A Wayback URL is unwrapped to its original: the agent does not get to pick the
    capture timestamp, the as-of does.
    """
    url = (url or "").strip()
    if not url:
        return None
    m = _WAYBACK_RE.match(url)
    if m:
        url = m.group(2)
    if not url.startswith(("http://", "https://")):
        if "://" in url:
            return None
        url = "https://" + url
    return url


def parse_wayback_location(location: str) -> tuple[datetime.datetime, str] | None:
    """Capture time and original URL of a Wayback redirect target, if it is one."""
    m = _WAYBACK_RE.match(location.strip())
    if not m:
        return None
    return parse_wayback_timestamp(m.group(1)), m.group(2)


# ---------------------------------------------------------------- content audit

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december|"
    "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_MDY_RE = re.compile(rf"\b({_MONTHS})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I)
_DMY_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MONTHS})\.?,?\s+(\d{{4}})\b", re.I)


def _month_number(name: str) -> int:
    key = name.lower()[:3]
    return [
        "jan", "feb", "mar", "apr", "may", "jun",
        "jul", "aug", "sep", "oct", "nov", "dec",
    ].index(key) + 1  # fmt: skip


def future_dated_mentions(
    text: str, as_of: datetime.datetime, limit: int = 10
) -> list[str]:
    """Dates in ``text`` later than the as-of day.

    An audit flag, not a block: a capture taken before the as-of can legitimately mention
    a scheduled future event (a meeting date, a maturity).
    """
    as_of_day = as_of.astimezone(datetime.timezone.utc).date()
    found: list[str] = []
    candidates = []
    for m in _ISO_DATE_RE.finditer(text):
        candidates.append(
            (m.group(0), int(m.group(1)), int(m.group(2)), int(m.group(3)))
        )
    for m in _MDY_RE.finditer(text):
        candidates.append(
            (m.group(0), int(m.group(3)), _month_number(m.group(1)), int(m.group(2)))
        )
    for m in _DMY_RE.finditer(text):
        candidates.append(
            (m.group(0), int(m.group(3)), _month_number(m.group(2)), int(m.group(1)))
        )
    for raw, y, mo, d in candidates:
        try:
            day = datetime.date(y, mo, d)
        except ValueError:
            continue
        if day > as_of_day and raw not in found:
            found.append(raw)
            if len(found) >= limit:
                break
    return found
