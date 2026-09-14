#!/usr/bin/env python3
"""Scan MiroFlow task JSONs for API / tool errors hidden inside tool results.

Tool failures are returned to the agent as tool results and never reach run.out, so a
run can look healthy while a tool fails on every call (E35, 2026-09-13). Used by
launch_gated.sh as the smoke gate (strict), and by run_guard.sh as the in-run tripwire.

Blocking rules (see api_error_signatures.py for what counts as OUR API failing):
  persistent categories (auth, quota, model): any new instance blocks.
  transient categories (rate, server, conn, request400): block when new instances of the
    same server+category appear in at least --transient-min-tasks distinct tasks
    (default 1 = strict, the smoke gate; the in-run guard uses 3).

Usage:
  scan_trace_errors.py <run_dir> [--since EPOCH] [--recursive] [--state FILE]
                       [--transient-min-tasks N] [--waive SERVER:CATEGORY=reason ...]
  --state FILE  remember error instances already reported; only NEW ones can block, so a
                run resumed after an acknowledged anomaly does not re-trigger on the same
                in-flight histories.
Exit: 0 = no blocking errors, 1 = blocking errors, 2 = usage error.
"""
import argparse
import collections
import glob
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_error_signatures import PERSISTENT, classify  # noqa: E402

SERVER = re.compile(r"<server_name>\s*([^<\s]+)\s*</server_name>")
TOOL = re.compile(r"<tool_name>\s*([^<\s]+)\s*</tool_name>")
SEGMENT = re.compile(r"Valid tool call (\d+) result:")


def text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join((x.get("text") or "") if isinstance(x, dict) else str(x) for x in c)
    return str(c or "")


def sessions(j):
    mh = j.get("main_agent_message_history") or {}
    yield "main", (mh.get("message_history") if isinstance(mh, dict) else mh) or []
    subs = j.get("sub_agent_message_history_sessions") or {}
    items = subs.items() if isinstance(subs, dict) else enumerate(subs)
    for sid, h in items:
        yield f"sub:{sid}", (h.get("message_history") if isinstance(h, dict) else h) or []


def tool_calls(msg):
    """Ordered (server, tool) pairs requested by an assistant message."""
    t = text(msg.get("content"))
    pairs = list(zip(SERVER.findall(t), TOOL.findall(t)))
    for tc in msg.get("tool_calls") or []:
        fn = (tc.get("function") or {}).get("name") if isinstance(tc, dict) else None
        if fn:
            pairs.append(("?", fn))
    return pairs


def segments(body, pending):
    """Split a tool-result message into per-call segments attributed to (server, tool)."""
    parts = SEGMENT.split(body)
    if len(parts) >= 3:  # [preamble, n1, text1, n2, text2, ...]
        for k in range(1, len(parts) - 1, 2):
            n = int(parts[k])
            srv, tl = pending[n - 1] if 0 < n <= len(pending) else ("?", "?")
            yield srv, tl, parts[k + 1]
        return
    if len(pending) == 1:
        yield pending[0][0], pending[0][1], body
    else:
        yield ",".join(sorted({s for s, _ in pending})) or "?", ",".join(sorted({t for _, t in pending})) or "?", body


def scan_file(path):
    """Yield (scope, index, server, tool, category, blocking, sample) for each error-looking tool result."""
    j = json.load(open(path, encoding="utf-8"))
    for scope, msgs in sessions(j):
        pending = []
        for i, m in enumerate(msgs):
            role = m.get("role")
            if role == "assistant":
                pending = tool_calls(m)
                continue
            if role not in ("user", "tool") or not pending:
                continue
            for srv, tl, seg in segments(text(m.get("content")), pending):
                cat, blocking = classify(seg, head_chars=6000, server=srv if "," not in srv else None)
                if cat:
                    yield scope, i, srv, tl, cat, blocking, seg.strip()[:240].replace("\n", " ")
            pending = []


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir")
    ap.add_argument("--since", type=float, default=0.0, help="only task files modified after this epoch")
    ap.add_argument("--recursive", action="store_true", help="include subfolders such as _aborted_*")
    ap.add_argument("--state", default="", help="JSON file of already-reported error instances")
    ap.add_argument("--transient-min-tasks", type=int, default=1, help="distinct tasks needed before a transient category blocks")
    ap.add_argument("--waive", action="append", default=[], help="SERVER:CATEGORY=reason (an explained, accepted condition)")
    a = ap.parse_args()

    waivers = {}
    for w in a.waive:
        key = w.split("=", 1)[0]
        if "=" not in w or ":" not in key:
            print(f"bad --waive {w!r}; expected SERVER:CATEGORY=reason")
            return 2
        waivers[key.strip()] = w.split("=", 1)[1].strip()

    seen = set()
    if a.state and os.path.exists(a.state):
        try:
            seen = set(json.load(open(a.state)))
        except Exception:  # noqa: BLE001
            seen = set()

    pattern = "**/task_*_attempt_*.json" if a.recursive else "task_*_attempt_*.json"
    files = [f for f in glob.glob(os.path.join(a.run_dir, pattern), recursive=a.recursive)
             if os.path.getmtime(f) > a.since]
    groups = collections.defaultdict(lambda: {"n": 0, "new": 0, "tasks": set(), "new_tasks": set(), "sample": ""})
    unreadable = []
    for f in sorted(files):
        tid = os.path.basename(f)[5:13]
        try:
            for scope, idx, srv, tl, cat, blocking, sample in scan_file(f):
                inst = f"{tid}|{scope}|{idx}|{srv}|{tl}|{cat}|" + hashlib.sha1(sample.encode()).hexdigest()[:10]
                g = groups[(srv, tl, cat, blocking)]
                g["n"] += 1
                g["tasks"].add(tid)
                g["sample"] = g["sample"] or sample
                if inst not in seen:
                    g["new"] += 1
                    g["new_tasks"].add(tid)
                    seen.add(inst)
        except Exception as e:  # noqa: BLE001
            unreadable.append((tid, str(e)[:80]))

    blocking_groups = 0
    print(f"scan_trace_errors: {len(files)} task files in {a.run_dir}"
          + (f" (since {a.since:.0f})" if a.since else "") + f" | transient threshold {a.transient_min_tasks} task(s)")
    order = lambda kv: (not kv[0][3], kv[0][2] not in PERSISTENT, -kv[1]["n"])  # noqa: E731
    for (srv, tl, cat, blocking), g in sorted(groups.items(), key=order):
        waived = next((r for k, r in waivers.items() if k in (f"{srv}:{cat}", f"*:{cat}", f"{srv}:*")), None)
        if not blocking:
            tag = "note"
        elif waived:
            tag = "WAIVED"
        elif cat in PERSISTENT and g["new"]:
            tag = "BLOCK"
        elif cat not in PERSISTENT and len(g["new_tasks"]) >= a.transient_min_tasks:
            tag = "BLOCK"
        elif g["new"]:
            tag = "watch"
        else:
            tag = "seen"
        blocking_groups += tag == "BLOCK"
        kind = "persistent" if cat in PERSISTENT else ("transient" if blocking else "")
        print(f"  {tag:<6} {cat:<10} {kind:<10} {g['n']:4d}x (new {g['new']}, new tasks {len(g['new_tasks'])})  {srv} / {tl}"
              + (f"  waiver: {waived}" if waived else ""))
        print(f"         e.g. {g['sample'][:210]!r}")
    for tid, err in unreadable:
        print(f"  note   unreadable {tid}: {err}")
    if a.state:
        json.dump(sorted(seen), open(a.state, "w"))
    if blocking_groups:
        print(f"TRACE GATE FAIL: {blocking_groups} blocking error group(s). Stop and investigate before continuing.")
        return 1
    print("TRACE GATE PASS: no blocking API/tool errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
