# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""Point-in-time macro data from ALFRED (FRED real-time API).

Every request is pinned to the vintage in effect at the task's as-of
(realtime_start = realtime_end = vintage), so the agent sees the values as they were
known then, not today's revised history. The as-of comes from the AS_OF env var set by
the harness; without it every tool refuses to run.
"""

import asyncio
import json
import os

import httpx
from fastmcp import FastMCP

from src.logging.logger import setup_mcp_logging
from src.tool.asof import current_as_of, format_as_of, with_provenance
from src.tool.mcp_servers.utils.asof_rules import (
    FRED_SERIES_FIELDS,
    fred_vintage_date,
    filter_fred_observations,
)

setup_mcp_logging(tool_name=os.path.basename(__file__))
mcp = FastMCP("asof-fred-mcp-server")

FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE_URL = os.environ.get("FRED_BASE_URL", "https://api.stlouisfed.org/fred")
MAX_OBSERVATIONS = 1000
SOURCE = "ALFRED / FRED real-time API (St. Louis Fed)"
EVIDENCE = (
    "ALFRED vintage dated {vintage}; a vintage counts as available only after its whole day "
    "(US Eastern) has ended"
)


async def _fred_get(endpoint: str, params: dict) -> dict:
    """GET a FRED endpoint as JSON, retrying rate limits and server errors."""
    query = {**params, "api_key": FRED_API_KEY, "file_type": "json"}
    last_error = ""
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(4):
            resp = await client.get(f"{FRED_BASE_URL}/{endpoint}", params=query)
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            if resp.status_code not in (429, 500, 502, 503, 504):
                break
            await asyncio.sleep(2**attempt)
    raise RuntimeError(last_error)


def _precheck() -> tuple[object, object] | str:
    as_of = current_as_of()
    if as_of is None:
        return "[ERROR]: No as-of time is set for this task; point-in-time data tools refuse to run without one."
    if not FRED_API_KEY:
        return "[ERROR]: FRED_API_KEY is not set."
    return as_of, fred_vintage_date(as_of)


def _public_url(endpoint: str, params: dict) -> str:
    """Request URL without the API key, for provenance."""
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{FRED_BASE_URL}/{endpoint}?{query}"


@mcp.tool()
async def fred_search_series(search_text: str, limit: int = 10) -> str:
    """Search FRED/ALFRED for economic data series (as the catalog stood at the current decision time).

    Args:
        search_text: Words describing the series, e.g. "nonfarm payrolls" or "real GDP".
        limit: Maximum number of series to return (1-50).

    Returns:
        Matching series ids with title, frequency, units and seasonal adjustment.
    """
    pre = _precheck()
    if isinstance(pre, str):
        return pre
    as_of, vintage = pre
    params = {
        "search_text": search_text,
        "limit": max(1, min(int(limit), 50)),
        "realtime_start": vintage.isoformat(),
        "realtime_end": vintage.isoformat(),
    }
    try:
        data = await _fred_get("series/search", params)
    except Exception as e:
        return f"[ERROR]: FRED request failed: {e}"
    rows = [{k: s.get(k) for k in FRED_SERIES_FIELDS} for s in data.get("seriess", [])]
    content = (
        json.dumps(rows, ensure_ascii=False, indent=1) if rows else "No series found."
    )
    return with_provenance(
        content,
        as_of=format_as_of(as_of),
        source=SOURCE,
        request=_public_url("series/search", params),
        first_available_at_evidence=EVIDENCE.format(vintage=vintage),
    )


@mcp.tool()
async def fred_get_observations(
    series_id: str,
    observation_start: str = "",
    observation_end: str = "",
    limit: int = 60,
) -> str:
    """Get the values of a FRED series exactly as they were published and known at the current decision time.

    Values are the vintage in effect at the decision time: later revisions and later releases are not visible.

    Args:
        series_id: FRED series id, e.g. "PAYEMS", "GDPC1", "CPIAUCSL".
        observation_start: Optional earliest observation date, YYYY-MM-DD.
        observation_end: Optional latest observation date, YYYY-MM-DD.
        limit: Number of most recent observations to return (1-1000).

    Returns:
        The vintage in effect, the latest release dates known at that time, and date/value rows in chronological order.
    """
    pre = _precheck()
    if isinstance(pre, str):
        return pre
    as_of, vintage = pre
    params = {
        "series_id": series_id.strip().upper(),
        "realtime_start": vintage.isoformat(),
        "realtime_end": vintage.isoformat(),
        "sort_order": "desc",
        "limit": max(1, min(int(limit), MAX_OBSERVATIONS)),
    }
    if observation_start:
        params["observation_start"] = observation_start
    if observation_end:
        # Observations dated after the vintage cannot exist yet.
        params["observation_end"] = min(observation_end, vintage.isoformat())
    try:
        series, obs_data, vintages = await asyncio.gather(
            _fred_get(
                "series",
                {
                    "series_id": params["series_id"],
                    "realtime_start": vintage.isoformat(),
                    "realtime_end": vintage.isoformat(),
                },
            ),
            _fred_get("series/observations", params),
            _fred_get(
                "series/vintagedates",
                {
                    "series_id": params["series_id"],
                    "realtime_start": "1776-07-04",
                    "realtime_end": vintage.isoformat(),
                    "sort_order": "desc",
                    "limit": 5,
                },
            ),
        )
    except Exception as e:
        return f"[ERROR]: FRED request failed: {e}"

    observations, dropped = filter_fred_observations(
        obs_data.get("observations", []), vintage
    )
    observations.reverse()
    info = (series.get("seriess") or [{}])[0]
    header = {k: info.get(k) for k in FRED_SERIES_FIELDS}
    lines = [
        json.dumps(header, ensure_ascii=False),
        f"latest_release_dates_known: {', '.join(vintages.get('vintage_dates', []))}",
        "date\tvalue",
    ]
    lines += [f"{o['date']}\t{o['value']}" for o in observations]
    if not observations:
        lines.append(
            "(no observations were published in this range by the decision time)"
        )
    return with_provenance(
        "\n".join(lines),
        as_of=format_as_of(as_of),
        source=SOURCE,
        vintage_in_effect=vintage.isoformat(),
        request=_public_url("series/observations", params),
        first_available_at_evidence=EVIDENCE.format(vintage=vintage),
        guard_dropped_rows=dropped or None,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
