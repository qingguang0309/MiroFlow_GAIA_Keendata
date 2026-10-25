# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the as-of layer. Run: .venv/bin/python -m unittest discover -s tests -v"""

import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

import httpx
from mcp import StdioServerParameters

from src.tool import asof
from src.tool.mcp_servers.utils import asof_rules as rules

UTC = datetime.timezone.utc


class AsOfParsingTest(unittest.TestCase):
    def test_date_only_is_start_of_day_new_york(self):
        self.assertEqual(
            asof.parse_as_of("2024-03-08"),
            datetime.datetime(2024, 3, 8, 5, 0, tzinfo=UTC),
        )

    def test_explicit_offset_kept(self):
        self.assertEqual(
            asof.parse_as_of("2024-03-08T14:30:00Z"),
            datetime.datetime(2024, 3, 8, 14, 30, tzinfo=UTC),
        )

    def test_context_sets_and_resets(self):
        self.assertIsNone(asof.AS_OF_CONTEXT_VAR.get())
        with asof.as_of_context("2024-03-08"):
            self.assertEqual(asof.decision_date_str(), "2024-03-08")
        self.assertIsNone(asof.AS_OF_CONTEXT_VAR.get())

    def test_empty_context_is_noop(self):
        with asof.as_of_context(None) as v:
            self.assertIsNone(v)

    def test_env_fallback_for_tool_servers(self):
        with mock.patch.dict(os.environ, {asof.AS_OF_ENV: "2024-03-08T05:00:00Z"}):
            self.assertEqual(asof.current_as_of(), asof.parse_as_of("2024-03-08"))


class FredRulesTest(unittest.TestCase):
    def test_vintage_is_previous_eastern_day(self):
        # Start of 03-08 New York: everything through 03-07 is known.
        self.assertEqual(
            rules.fred_vintage_date(asof.parse_as_of("2024-03-08")),
            datetime.date(2024, 3, 7),
        )
        # 15:00 New York on 03-08: a vintage dated 03-08 may still be in the future.
        self.assertEqual(
            rules.fred_vintage_date(asof.parse_as_of("2024-03-08T15:00:00-05:00")),
            datetime.date(2024, 3, 7),
        )
        # 00:30 UTC on 03-08 is still 03-07 in New York.
        self.assertEqual(
            rules.fred_vintage_date(asof.parse_as_of("2024-03-08T00:30:00Z")),
            datetime.date(2024, 3, 6),
        )

    def test_filter_drops_rows_after_vintage(self):
        vintage = datetime.date(2024, 3, 7)
        obs = [
            {"realtime_start": "2024-02-02", "date": "2024-01-01", "value": "1"},
            {"realtime_start": "2024-03-08", "date": "2024-02-01", "value": "2"},
            {"realtime_start": "2024-03-01", "date": "2024-04-01", "value": "3"},
            {"date": "2024-01-01", "value": "4"},
        ]
        kept, dropped = rules.filter_fred_observations(obs, vintage)
        self.assertEqual([o["value"] for o in kept], ["1"])
        self.assertEqual(dropped, 3)


class WaybackRulesTest(unittest.TestCase):
    def test_wayback_url_is_unwrapped(self):
        self.assertEqual(
            rules.normalize_target_url(
                "https://web.archive.org/web/20250101000000/https://x.com/a"
            ),
            "https://x.com/a",
        )
        self.assertEqual(
            rules.normalize_target_url("web.archive.org/web/2025id_/http://x.com/a"),
            "http://x.com/a",
        )

    def test_scheme_rules(self):
        self.assertEqual(rules.normalize_target_url("x.com/a"), "https://x.com/a")
        self.assertIsNone(rules.normalize_target_url("ftp://x.com/a"))
        self.assertIsNone(rules.normalize_target_url("  "))

    def test_parse_location(self):
        ts, orig = rules.parse_wayback_location(
            "https://web.archive.org/web/20240305120000id_/https://x.com/a"
        )
        self.assertEqual(ts, datetime.datetime(2024, 3, 5, 12, tzinfo=UTC))
        self.assertEqual(orig, "https://x.com/a")
        self.assertIsNone(rules.parse_wayback_location("https://x.com/a"))

    def test_future_dated_mentions(self):
        as_of = asof.parse_as_of("2024-03-08")
        text = "Posted 2024-03-01. Updated March 20, 2024. Meeting on 5 Apr 2024. Old: Jan 2, 2023."
        self.assertEqual(
            rules.future_dated_mentions(text, as_of), ["March 20, 2024", "5 Apr 2024"]
        )


class ServerParamsTest(unittest.TestCase):
    def test_params_copied_not_mutated(self):
        from src.logging.logger import TASK_CONTEXT_VAR
        from src.tool.manager import update_server_params_with_context_var

        shared = StdioServerParameters(command="python", args=[], env={"K": "v"})
        token = TASK_CONTEXT_VAR.set("t1")
        try:
            with asof.as_of_context("2024-03-08"):
                updated = update_server_params_with_context_var(shared)
        finally:
            TASK_CONTEXT_VAR.reset(token)
        self.assertEqual(updated.env["AS_OF"], "2024-03-08T05:00:00Z")
        self.assertEqual(updated.env["TASK_ID"], "t1")
        self.assertEqual(updated.env["K"], "v")
        self.assertEqual(shared.env, {"K": "v"})

    def test_no_as_of_without_context(self):
        from src.tool.manager import update_server_params_with_context_var

        shared = StdioServerParameters(command="python", args=[], env=None)
        self.assertNotIn("AS_OF", update_server_params_with_context_var(shared).env)


class RecordingTest(unittest.IsolatedAsyncioTestCase):
    async def test_records_calls_in_order_with_key(self):
        from src.logging.logger import TASK_CONTEXT_VAR
        from src.tool.manager import ToolManager
        from src.tool.recorder import ToolCallRecorder, call_key

        with tempfile.TemporaryDirectory() as d:
            recorder = ToolCallRecorder(d, tags={"condition": "D3"})
            main = ToolManager([], agent_name="main", recorder=recorder)
            sub = ToolManager([], agent_name="agent-worker", recorder=recorder)

            async def ok(server_name, tool_name, arguments):
                return {
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "result": "R",
                }

            async def boom(server_name, tool_name, arguments):
                raise TimeoutError("slow")

            main._execute_tool_call = ok
            sub._execute_tool_call = boom
            token = TASK_CONTEXT_VAR.set("task-1")
            try:
                path = recorder.begin_task("task-1")
                with asof.as_of_context("2024-03-08"):
                    await main.execute_tool_call(
                        "tool-asof-fred",
                        "fred_get_observations",
                        {"series_id": "PAYEMS"},
                    )
                    with self.assertRaises(TimeoutError):
                        await sub.execute_tool_call(
                            "tool-asof-fetch", "fetch_page_as_of", {"url": "x"}
                        )
            finally:
                TASK_CONTEXT_VAR.reset(token)

            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([r["seq"] for r in rows], [0, 1])
            self.assertEqual([r["agent"] for r in rows], ["main", "agent-worker"])
            self.assertEqual(rows[0]["as_of"], "2024-03-08T05:00:00Z")
            self.assertEqual(rows[0]["response"]["result"], "R")
            self.assertIn("TimeoutError", rows[1]["response"]["error"])
            self.assertEqual(
                rows[0]["call_key"],
                call_key(
                    "tool-asof-fred",
                    "fred_get_observations",
                    {"series_id": "PAYEMS"},
                    "2024-03-08T05:00:00Z",
                    {"condition": "D3"},
                ),
            )
            # Same call under another condition or as-of must not share a key.
            self.assertNotEqual(
                rows[0]["call_key"],
                call_key(
                    "tool-asof-fred",
                    "fred_get_observations",
                    {"series_id": "PAYEMS"},
                    "2024-03-08T05:00:00Z",
                    {"condition": "D0"},
                ),
            )

    async def test_no_recorder_passthrough(self):
        from src.tool.manager import ToolManager

        tm = ToolManager([])

        async def ok(server_name, tool_name, arguments):
            return {"result": "R"}

        tm._execute_tool_call = ok
        self.assertEqual(await tm.execute_tool_call("s", "t", {}), {"result": "R"})


def _mock_client_factory(handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    return factory


class FetchServerTest(unittest.IsolatedAsyncioTestCase):
    """The leak guards of fetch_page_as_of, against a fake Wayback."""

    @classmethod
    def setUpClass(cls):
        from src.tool.mcp_servers import asof_fetch_mcp_server as srv

        cls.srv = srv
        cls.fn = staticmethod(getattr(srv.fetch_page_as_of, "fn", srv.fetch_page_as_of))

    async def _run(self, handler, url="https://x.com/a", as_of="2024-03-08"):
        with (
            mock.patch.object(
                self.srv.httpx, "AsyncClient", _mock_client_factory(handler)
            ),
            mock.patch.object(self.srv.asyncio, "sleep", mock.AsyncMock()),
            asof.as_of_context(as_of),
        ):
            return await self.fn(url)

    @staticmethod
    def _cdx(ts):
        return httpx.Response(
            200,
            json=[
                ["timestamp", "original", "mimetype", "digest"],
                [ts, "https://x.com/a", "text/html", "D"],
            ],
        )

    async def test_fails_closed_without_as_of(self):
        out = await self.fn("https://x.com/a")
        self.assertTrue(out.startswith("[ERROR]: No as-of"))

    async def test_cdx_bounded_by_as_of_and_page_served(self):
        seen = {}

        def handler(req):
            if req.url.path.startswith("/cdx"):
                seen["to"] = req.url.params["to"]
                return self._cdx("20240301120000")
            self.assertIn("/web/20240301120000id_/", str(req.url))
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                html="<html><script>x()</script><p>Rates held. Next meeting March 20, 2024.</p></html>",
            )

        out = await self._run(handler)
        self.assertEqual(seen["to"], "20240308050000")
        self.assertIn("captured_at: 2024-03-01T12:00:00Z", out)
        self.assertIn("Rates held.", out)
        self.assertNotIn("x()", out)
        self.assertIn("audit_future_dated_mentions: March 20, 2024", out)

    async def test_redirect_to_later_capture_refused(self):
        def handler(req):
            if req.url.path.startswith("/cdx"):
                return self._cdx("20240301120000")
            if "20240301120000" in str(req.url):
                return httpx.Response(
                    302, headers={"location": "/web/20250101000000id_/https://x.com/a"}
                )
            raise AssertionError("must not fetch a capture after the as-of")

        out = await self._run(handler)
        self.assertTrue(out.startswith("[NOT AVAILABLE]"))

    async def test_redirect_to_earlier_capture_followed(self):
        def handler(req):
            if req.url.path.startswith("/cdx"):
                return self._cdx("20240301120000")
            if "20240301120000" in str(req.url):
                return httpx.Response(
                    302, headers={"location": "/web/20240201000000id_/https://x.com/b"}
                )
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, text="earlier page"
            )

        out = await self._run(handler)
        self.assertIn("captured_at: 2024-02-01T00:00:00Z", out)
        self.assertIn("earlier page", out)

    async def test_capture_after_as_of_from_cdx_is_rejected(self):
        def handler(req):
            if req.url.path.startswith("/cdx"):
                return self._cdx("20240310000000")  # CDX misbehaves
            raise AssertionError("must not fetch")

        self.assertTrue((await self._run(handler)).startswith("[NOT AVAILABLE]"))

    async def test_no_capture(self):
        def handler(req):
            return httpx.Response(200, json=[])

        self.assertTrue((await self._run(handler)).startswith("[NOT AVAILABLE]"))


if __name__ == "__main__":
    unittest.main()
