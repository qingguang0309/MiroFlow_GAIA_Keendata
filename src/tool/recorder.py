# SPDX-FileCopyrightText: 2025 MiromindAI
#
# SPDX-License-Identifier: Apache-2.0

"""Tool-call recording: every tool response of a task, in call order, one JSONL per task.

This is the base for replay and counterfactual intervention (W2): a recorded prefix can
be replayed verbatim, and the response at one position swapped. ``call_key`` identifies
a call independent of when it ran, so it already includes the as-of and the experiment
tags (condition, corpus version, filter version): runs under different conditions must
never share a cached response.
"""

import datetime
import json
import pathlib
import threading
from typing import Any

from src.tool.asof import current_as_of, format_as_of, sha256_text


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def call_key(
    server_name: str, tool_name: str, arguments: dict, as_of: str | None, tags: dict
) -> str:
    return sha256_text(_canonical([server_name, tool_name, arguments, as_of, tags]))


class ToolCallRecorder:
    def __init__(self, record_dir: str | pathlib.Path, tags: dict | None = None):
        self.record_dir = pathlib.Path(record_dir)
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.tags = dict(tags or {})
        self._seq: dict[str, int] = {}
        self._paths: dict[str, pathlib.Path] = {}
        self._lock = threading.Lock()

    def begin_task(self, task_id: str) -> pathlib.Path:
        """Start a fresh record file for one run of ``task_id`` (retries get their own file)."""
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        with self._lock:
            self._seq[task_id] = 0
            self._paths[task_id] = self.record_dir / f"{task_id}__{stamp}.jsonl"
            return self._paths[task_id]

    def path_for(self, task_id: str) -> pathlib.Path:
        if task_id not in self._paths:
            self._paths[task_id] = self.record_dir / f"{task_id}.jsonl"
        return self._paths[task_id]

    def record(
        self,
        *,
        agent_name: str,
        server_name: str,
        tool_name: str,
        arguments: dict,
        response: Any,
        started_at: datetime.datetime,
        duration_ms: int,
    ) -> None:
        from src.logging.logger import TASK_CONTEXT_VAR

        task_id = TASK_CONTEXT_VAR.get() or "no_task"
        as_of_dt = current_as_of()
        as_of = format_as_of(as_of_dt) if as_of_dt else None
        if isinstance(response, dict):
            payload = response.get("result", response.get("error", ""))
        else:
            payload = response
        with self._lock:
            seq = self._seq.get(task_id, 0)
            self._seq[task_id] = seq + 1
            entry = {
                "seq": seq,
                "task_id": task_id,
                "agent": agent_name,
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments,
                "as_of": as_of,
                "tags": self.tags,
                "call_key": call_key(
                    server_name, tool_name, arguments, as_of, self.tags
                ),
                "response": response,
                "response_sha256": sha256_text(str(payload)),
                "started_at": started_at.isoformat(),
                "duration_ms": duration_ms,
            }
            with open(self.path_for(task_id), "a", encoding="utf-8") as f:
                f.write(_canonical(entry) + "\n")
