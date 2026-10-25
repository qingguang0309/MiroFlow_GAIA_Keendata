# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""Point-in-time web page reading from the Internet Archive Wayback Machine.

A page is served only from the latest Wayback capture taken at or before the task's
as-of (AS_OF env var, set by the harness). There is no live-web fallback. Redirects are
followed by hand and every hop's capture time is checked, because Wayback silently
redirects to the *nearest* capture, which can be later than the one asked for.

"Never archived" and "archived only after the as-of" return the same message: the CDX
query is bounded by the as-of, so later captures are never even looked up.
"""

import asyncio
import io
import os

import httpx
from bs4 import BeautifulSoup
from fastmcp import FastMCP

from src.logging.logger import setup_mcp_logging
from src.tool.asof import current_as_of, format_as_of, with_provenance
from src.tool.mcp_servers.utils.asof_rules import (
    future_dated_mentions,
    normalize_target_url,
    parse_wayback_location,
    parse_wayback_timestamp,
    wayback_timestamp,
)

setup_mcp_logging(tool_name=os.path.basename(__file__))
mcp = FastMCP("asof-fetch-mcp-server")

CDX_URL = "https://web.archive.org/cdx/search/cdx"
MAX_CHARS = int(os.environ.get("ASOF_FETCH_MAX_CHARS", "40000"))
MAX_REDIRECTS = 5
SOURCE = "Internet Archive Wayback Machine"


async def _get(client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
    """GET with retries on rate limits, server errors and connection failures."""
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            resp = await client.get(url, **kwargs)
            if resp.status_code not in (429, 500, 502, 503, 504):
                return resp
            last_exc = RuntimeError(f"HTTP {resp.status_code}")
        except httpx.TransportError as e:
            last_exc = e
        await asyncio.sleep(2**attempt)
    raise RuntimeError(f"Wayback request failed: {last_exc}")


async def _latest_capture(client, url: str, as_of) -> dict | None:
    """Latest 200-status capture of ``url`` at or before ``as_of``."""
    params = {
        "url": url,
        "to": wayback_timestamp(as_of),
        "output": "json",
        "fl": "timestamp,original,mimetype,digest",
        "filter": "statuscode:200",
        "limit": "-1",
        "fastLatest": "true",
    }
    resp = await _get(client, CDX_URL, params=params)
    if resp.status_code != 200 or not resp.text.strip():
        return None
    rows = resp.json()
    if len(rows) < 2:  # first row is the header
        return None
    capture = dict(zip(rows[0], rows[-1]))
    if parse_wayback_timestamp(capture["timestamp"]) > as_of:
        return None
    return capture


def _to_text(content: bytes, mimetype: str) -> str:
    if "html" in mimetype or "xml" in mimetype or not mimetype:
        soup = BeautifulSoup(content, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
            tag.decompose()
        lines = [ln.strip() for ln in soup.get_text("\n").splitlines()]
        return "\n".join(ln for ln in lines if ln)
    if mimetype.startswith("text/") or "json" in mimetype:
        return content.decode("utf-8", errors="replace")
    from markitdown import MarkItDown

    ext = {"application/pdf": ".pdf"}.get(mimetype, "")
    return (
        MarkItDown()
        .convert_stream(io.BytesIO(content), file_extension=ext)
        .text_content
    )


@mcp.tool()
async def fetch_page_as_of(url: str) -> str:
    """Read a web page as it existed at the current decision time.

    The page is served from the latest web-archive capture taken at or before the decision time.
    Pages that were not archived by then cannot be read. Live web access is not available.

    Args:
        url: The page URL, e.g. "https://www.federalreserve.gov/newsevents/pressreleases.htm".

    Returns:
        The page text, preceded by a provenance block with the capture time.
    """
    as_of = current_as_of()
    if as_of is None:
        return "[ERROR]: No as-of time is set for this task; point-in-time reading tools refuse to run without one."
    target = normalize_target_url(url)
    if target is None:
        return f"[ERROR]: Unsupported URL: '{url}'. Only http(s) pages can be read."
    not_available = (
        f"[NOT AVAILABLE]: No archived copy of '{target}' exists at or before the decision time "
        f"({format_as_of(as_of)}). Try another source for this information."
    )

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            capture = await _latest_capture(client, target, as_of)
            if capture is None:
                return not_available

            capture_ts = parse_wayback_timestamp(capture["timestamp"])
            fetch_url = f"https://web.archive.org/web/{capture['timestamp']}id_/{capture['original']}"
            for _ in range(MAX_REDIRECTS + 1):
                resp = await _get(client, fetch_url)
                if resp.status_code not in (301, 302, 303, 307, 308):
                    break
                hop = parse_wayback_location(
                    str(httpx.URL(fetch_url).join(resp.headers.get("location", "")))
                )
                if hop is None or hop[0] > as_of:
                    # Redirect leaves the archive or lands on a capture after the as-of.
                    return not_available
                capture_ts = hop[0]
                fetch_url = f"https://web.archive.org/web/{wayback_timestamp(capture_ts)}id_/{hop[1]}"
            else:
                return not_available
            if resp.status_code != 200:
                return not_available

            mimetype = resp.headers.get("content-type", capture.get("mimetype", ""))
            text = _to_text(resp.content, mimetype.split(";")[0].strip().lower())
    except Exception as e:
        return f"[ERROR]: Failed to read archived page: {e}"

    truncated = len(text) > MAX_CHARS
    if truncated:
        text = text[:MAX_CHARS] + "\n\n[... truncated ...]"
    flags = future_dated_mentions(text, as_of)
    return with_provenance(
        text,
        as_of=format_as_of(as_of),
        source=SOURCE,
        requested_url=target,
        captured_at=format_as_of(capture_ts),
        archived_url=fetch_url,
        first_available_at_evidence="archive capture taken at captured_at, which is at or before as_of",
        cdx_digest=capture.get("digest"),
        truncated=truncated or None,
        audit_future_dated_mentions=(
            "; ".join(flags)
            + " (dates later than as_of found in the text; may be scheduled events)"
            if flags
            else None
        ),
    )


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
