"""Offline re-scoring check for OutputFormatter.normalize_answer_punct.

For every task JSON in the given run dirs, re-judge the stored final_boxed_answer
(raw) and its punctuation-normalized form with the same GAIA judge the run used
(utils.eval_utils.verify_answer_gaia), and report every task whose verdict
changes. Run from the MiroFlow root with the project venv:

    .venv/bin/python scripts_sol/rescore_normalized.py logs/gaia-val/<run_dir> [...]
"""
import asyncio, glob, json, os, sys

sys.path.insert(0, os.getcwd())
from src.utils.io_utils import OutputFormatter  # noqa: E402
from utils.eval_utils import verify_answer_gaia  # noqa: E402

norm = OutputFormatter.normalize_answer_punct
SAMPLES = [
    ("Russian–German Legion", "Russian-German Legion"),
    ("Zone 42 — Level B2", "Zone 42 - Level B2"),
    ("−5", "-5"),
    ("Sam’s “Home”", "Sam's \"Home\""),
    ("(¬A → B) ↔ (A ∨ ¬B)", "(¬A → B) ↔ (A ∨ ¬B)"),  # logic symbols untouched
    ("x ≤ 3, 1.456", "x ≤ 3, 1.456"),
    ("a​b c", "ab c"),
    ("", ""),
]
for raw, want in SAMPLES:
    got = norm(raw)
    assert got == want, (raw, got, want)
    assert norm(got) == got  # idempotent
print(f"unit samples OK ({len(SAMPLES)})")


async def rescore(run_dir):
    files = sorted(glob.glob(os.path.join(run_dir, "task_*_attempt_*.json")))
    changed_str = flips = mismatch = 0
    for f in files:
        j = json.load(open(f, encoding="utf-8"))
        gt, raw = str(j.get("ground_truth") or ""), str(j.get("final_boxed_answer") or "")
        stored = j.get("judge_result")
        n = norm(raw)
        v_raw = await verify_answer_gaia(gt, raw)
        v_norm = v_raw if n == raw else await verify_answer_gaia(gt, n)
        if stored in ("CORRECT", "INCORRECT") and v_raw != stored:
            mismatch += 1
            print(f"  [judge drift] {j['task_id'][:8]} stored={stored} rejudged={v_raw}")
        if n != raw:
            changed_str += 1
            print(f"  [normalized] {j['task_id'][:8]} {raw!r} -> {n!r}  {v_raw} -> {v_norm}")
        if v_raw != v_norm:
            flips += 1
    print(f"{run_dir}: {len(files)} tasks | strings changed {changed_str} | verdict flips {flips} | stored-vs-rejudged drift {mismatch}")


async def main():
    for d in sys.argv[1:]:
        await rescore(d)


asyncio.run(main())
