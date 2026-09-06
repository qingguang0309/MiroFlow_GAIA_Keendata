#!/usr/bin/env python3
"""G1: confidence-gated adaptive resampling for a MiroFlow GAIA run.

Idea: the final-answer extractor's **Confidence** is well calibrated for kimi-k3
(f2: conf>=90 -> 98% correct, 80-89 -> 81%, <80 -> ~50%). So instead of
re-running all 165 tasks k times, re-run only the low-confidence tasks (k-1 extra
samples) and majority-vote (official-scorer clustering, confidence tie-break) on
those; every other task keeps its single-run answer.

Usage (run on the server, cwd = ~/MiroFlow):
  # 1) plan + create subset dataset/configs, launch r2..rk in tmux (with monitors)
  python3 scripts_sol/adaptive_resample.py launch --run-dir logs/gaia-val/<r1> \
      --config agent_gaia-validation-keendata-kimi --threshold 85 --k 3 [--concurrency 5] [--dry-run]
  # 2) after r2..rk finish: vote + instability report
  python3 scripts_sol/adaptive_resample.py report --run-dir logs/gaia-val/<r1> --votekit /tmp/votekit

Selection rule: confidence < threshold OR confidence missing. Tasks in r1 that are
not judged/boxed are also selected (they need a sample anyway).
No question text is written anywhere except the subset dataset file (same content as
data/gaia-val, which is gitignored).
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys

CONF_PAT = re.compile(r"\*\*Confidence:?\*\*:?\s*\[?(\d{1,3})\]?")


def load_r1(run_dir):
    out = {}
    for f in glob.glob(os.path.join(run_dir, "task_*_attempt_1.json")):
        try:
            j = json.load(open(f))
        except Exception:
            continue
        raw = json.dumps(j)
        confs = CONF_PAT.findall(raw)
        out[j["task_id"]] = {
            "conf": int(confs[-1]) if confs else None,
            "ok": j.get("judge_result") == "CORRECT",
            "judged": j.get("judge_result") in ("CORRECT", "INCORRECT"),
            "boxed": bool((j.get("final_boxed_answer") or "").strip()),
            "steps": len(j.get("step_logs", [])),
        }
    return out


def select(r1, threshold):
    sel = []
    for tid, v in r1.items():
        if v["conf"] is None or v["conf"] < threshold or not v["judged"] or not v["boxed"]:
            sel.append(tid)
    return sorted(sel)


def tag_of(run_dir, threshold):
    base = os.path.basename(run_dir.rstrip("/"))
    return f"{base}_T{threshold}"


def cmd_launch(a):
    r1 = load_r1(a.run_dir)
    if not r1:
        sys.exit(f"no task files in {a.run_dir}")
    sel = select(r1, a.threshold)
    n_wrong_sel = sum(1 for t in sel if not r1[t]["ok"])
    n_wrong = sum(1 for v in r1.values() if not v["ok"])
    print(f"r1: {len(r1)} tasks, {sum(v['ok'] for v in r1.values())} correct")
    print(f"selected (conf<{a.threshold} or unjudged): {len(sel)} tasks = {100*len(sel)/len(r1):.0f}% "
          f"| covers {n_wrong_sel}/{n_wrong} of r1's wrong answers (selection is by confidence only, never by correctness)")
    tag = tag_of(a.run_dir, a.threshold)
    data_dir = f"data/gaia-val-resample/{tag}"
    bench_name = f"gaia-val-resample-{tag}"
    agent_name = f"{a.config}-resample-{tag}"
    if a.dry_run:
        print(f"[dry-run] would write {data_dir}/standardized_data.jsonl, config/benchmark/{bench_name}.yaml, "
              f"config/{agent_name}.yaml and launch r2..r{a.k} -> {a.run_dir}_r{{2..{a.k}}}")
        for t in sel:
            v = r1[t]
            print(f"   {t[:8]} conf={v['conf']} steps={v['steps']} {'✓' if v['ok'] else '✗'}")
        return
    os.makedirs(data_dir, exist_ok=True)
    keep = set(sel)
    n = 0
    with open(f"{data_dir}/standardized_data.jsonl", "w") as out:
        for line in open("data/gaia-val/standardized_data.jsonl"):
            if json.loads(line)["task_id"] in keep:
                out.write(line); n += 1
    assert n == len(sel), f"dataset rows {n} != selected {len(sel)}"
    bench_src = open("config/benchmark/gaia-validation.yaml").read()
    bench = bench_src.replace('name: "gaia-validation"', f'name: "{bench_name}"') \
                     .replace('data_dir: "${data_dir}/gaia-validation"', f'data_dir: "${{data_dir}}/gaia-val-resample/{tag}"') \
                     .replace('data_dir: "${data_dir}/gaia-val"', f'data_dir: "${{data_dir}}/gaia-val-resample/{tag}"')
    open(f"config/benchmark/{bench_name}.yaml", "w").write(bench)
    agent_src = open(f"config/{a.config}.yaml").read()
    agent = re.sub(r"^(\s*- benchmark:\s*).*$", rf"\g<1>{bench_name}", agent_src, count=1, flags=re.M)
    open(f"config/{agent_name}.yaml", "w").write(agent)
    print(f"wrote {data_dir} ({n} tasks), config/benchmark/{bench_name}.yaml, config/{agent_name}.yaml")
    env = "AUX_LLM_CONCURRENCY=2 FORCE_FIRST_TOOL_CALL=1"
    for r in range(2, a.k + 1):
        out_dir = f"{a.run_dir.rstrip('/')}_r{r}"
        os.makedirs(out_dir, exist_ok=True)
        sess = f"rs_r{r}"
        cmd = (f"cd ~/MiroFlow && {env} ~/.local/bin/uv run main.py common-benchmark "
               f"--config_file_name={agent_name} output_dir={out_dir} "
               f"benchmark.execution.max_concurrent={a.concurrency} > {out_dir}/run.out 2>&1")
        subprocess.run(["tmux", "new-session", "-d", "-s", sess, cmd], check=True)
        mon = (f"cd ~/MiroFlow && ARM_MATCH={agent_name} MON_TAG=rs_r{r} LOAD_GATE=0 "
               f"PROBE_URL=https://api.moonshot.cn/v1/chat/completions PROBE_MODEL=kimi-k3 PROBE_KEY_ENV=KIMI_API_KEY "
               f"RUNOUT_GLOB={out_dir}/run.out python3 scripts_sol/monitor_sol.py --dirs {out_dir} "
               f">> logs/gaia-val/monitor_rs_r{r}.out 2>&1")
        subprocess.run(["tmux", "new-session", "-d", "-s", f"mon_{sess}", mon], check=True)
        print(f"launched r{r}: tmux {sess} (+ mon_{sess}) -> {out_dir}")
    print("remember: arm the Moonshot balance guard (FLOOR=25 bash /tmp/balance_guard.sh with pgrep pattern "
          f"'{agent_name}') and sweep contaminated tasks before voting.")


def cmd_report(a):
    sys.path.insert(0, a.votekit)
    from src.voting.vote import _load_run, vote_task  # noqa
    from src.scoring.scorer import question_scorer  # noqa
    r1 = _load_run(a.run_dir)
    extra_dirs = sorted(glob.glob(a.run_dir.rstrip("/") + "_r[0-9]*"))
    runs = [r1] + [_load_run(d) for d in extra_dirs]
    names = ["r1"] + [os.path.basename(d).rsplit("_", 1)[-1] for d in extra_dirs]
    resampled = set().union(*[set(r) for r in runs[1:]]) if len(runs) > 1 else set()
    print(f"r1 tasks {len(r1)} | extra runs {names[1:]} covering {len(resampled)} tasks")
    single_c = sum(1 for t in r1 if r1[t]["correct"])
    voted_c = 0; flips_up = flips_down = 0
    stable_ok = stable_bad = unstable = 0
    detail = []
    for tid in sorted(r1):
        entries = [(i, runs[i][tid]["answer"], runs[i][tid]["confidence"]) for i in range(len(runs)) if tid in runs[i]]
        voted, clusters = vote_task(entries)
        truth = r1[tid]["truth"]
        vc = bool(question_scorer(voted, str(truth)))
        voted_c += vc
        if tid in resampled:
            outcomes = [runs[i][tid]["correct"] for i in range(len(runs)) if tid in runs[i]]
            if all(outcomes): stable_ok += 1
            elif not any(outcomes): stable_bad += 1
            else: unstable += 1
            if vc and not r1[tid]["correct"]: flips_up += 1
            if (not vc) and r1[tid]["correct"]: flips_down += 1
            detail.append((tid[:8], r1[tid]["confidence"], "".join("✓" if o else "✗" for o in outcomes), "✓" if vc else "✗"))
    n = len(r1)
    print(f"single-run (r1): {single_c}/{n} = {100*single_c/n:.1f}%")
    print(f"adaptive k<={len(runs)} vote: {voted_c}/{n} = {100*voted_c/n:.1f}%  (vs r1: +{flips_up} / -{flips_down})")
    print(f"resampled tasks: stable-correct {stable_ok} | stable-wrong {stable_bad} | UNSTABLE {unstable} "
          f"(instability rate {100*unstable/max(1,len(resampled)):.0f}%)")
    print("tid      conf  r1r2r3  voted")
    for d in detail:
        print(f"{d[0]}  {str(d[1]):>4}  {d[2]:<7} {d[3]}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("launch"); l.add_argument("--run-dir", required=True); l.add_argument("--config", required=True)
    l.add_argument("--threshold", type=int, default=85); l.add_argument("--k", type=int, default=3)
    l.add_argument("--concurrency", type=int, default=5); l.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("report"); r.add_argument("--run-dir", required=True); r.add_argument("--votekit", default="/tmp/votekit")
    a = ap.parse_args()
    {"launch": cmd_launch, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    main()
