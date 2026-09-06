#!/usr/bin/env python3
"""Smoke gate for a MiroFlow GAIA run directory.

Usage: check_smoke.py <run_dir> [--expect-model gpt-5.6-sol] [--min-tasks 2]
Exit 0 = pass, 1 = fail. Prints a short report (no question content).

Pass criteria:
  * >= min-tasks task_*.json files, every one status == completed with a non-empty
    final_boxed_answer and a judge_result
  * every task's usage logs name the expected model and never the bare model
    (e.g. "[GPT5OpenAIClient | gpt-5.6-sol]" present, "| gpt-5.6]" absent)
  * fewer than 3 upstream-unavailable errors (503) across the traces
"""
import glob
import json
import os
import re
import sys
from datetime import datetime

USAGE_PAT = re.compile(r"Usage log: \[(\w+) \| ([^\]]+)\]")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    run_dir = args[0]
    expect = "gpt-5.6-sol"
    min_tasks = 2
    require_text = None  # e.g. "URL Source:" = evidence that Jina Reader served scrape content
    if "--expect-model" in args:
        expect = args[args.index("--expect-model") + 1]
    if "--min-tasks" in args:
        min_tasks = int(args[args.index("--min-tasks") + 1])
    if "--require-text" in args:
        require_text = args[args.index("--require-text") + 1]

    files = sorted(glob.glob(os.path.join(run_dir, "task_*_attempt_*.json")))
    problems = []
    if len(files) < min_tasks:
        problems.append(f"only {len(files)} task files (< {min_tasks})")

    total_503 = 0
    total_429 = 0
    total_required = 0
    for f in files:
        try:
            j = json.load(open(f))
        except Exception as e:  # noqa: BLE001
            problems.append(f"{os.path.basename(f)[:13]}: unreadable ({e})")
            continue
        tid = str(j.get("task_id", "?"))[:8]
        raw = json.dumps(j)
        models = set(m.group(2).strip() for m in USAGE_PAT.finditer(raw))
        n_steps = len(j.get("step_logs", []))
        dur = ""
        if j.get("start_time") and j.get("end_time"):
            dur = f"{(datetime.fromisoformat(j['end_time']) - datetime.fromisoformat(j['start_time'])).total_seconds() / 60:.0f}min"
        n503 = raw.count("Service temporarily unavailable") + raw.count("Error code: 503")
        n429 = raw.count("Error code: 429") + raw.count("RateLimitError")
        total_503 += n503
        total_429 += n429
        nreq = raw.count(require_text) if require_text else 0
        total_required += nreq
        status = j.get("status")
        boxed = (j.get("final_boxed_answer") or "").strip()
        judge = j.get("judge_result")
        extra = f" {require_text!r}x{nreq}" if require_text else ""
        print(f"  {tid}: status={status} judge={judge} boxed={'yes' if boxed else 'EMPTY'} steps={n_steps} dur={dur} models={sorted(models)} 503={n503} 429={n429}{extra}")
        if status != "completed":
            problems.append(f"{tid}: status={status}")
        if not boxed:
            problems.append(f"{tid}: empty final_boxed_answer")
        if not judge:
            problems.append(f"{tid}: no judge_result")
        exp_list = [x.strip() for x in expect.split(",") if x.strip()]
        required, allowed = exp_list[0], set(exp_list)
        if not models:
            problems.append(f"{tid}: no usage logs found (no LLM calls?)")
        elif required not in models:
            problems.append(f"{tid}: expected model {required!r} not in {sorted(models)}")
        wrong = [m for m in models if m not in allowed]
        if wrong:
            problems.append(f"{tid}: unexpected model(s) {wrong}")
    if total_503 >= 3:
        problems.append(f"{total_503} upstream 503 errors across traces (router flapping)")
    if require_text and total_required == 0:
        problems.append(f"required text {require_text!r} never appeared in traces (expected scraper not active)")

    if problems:
        print("SMOKE FAIL:")
        for p in problems:
            print("   -", p)
        return 1
    print(f"SMOKE PASS: {len(files)} tasks completed, model={expect}, 503={total_503}, 429={total_429}" + (f", {require_text!r}x{total_required}" if require_text else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
