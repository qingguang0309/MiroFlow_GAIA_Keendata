#!/usr/bin/env python3
"""Server-side monitor for the sol 165x3 campaign (stdlib only).

Usage:
  monitor_sol.py --ts <TS>            # watches logs/gaia-val/full165_e{1,2,3}_sol_<TS>
  monitor_sol.py --dirs d1 d2 d3      # explicit run dirs
  add --once to print one report and exit.

Every 60 s : probe router (gpt-5.6-sol). 5xx / connection failure => SIGSTOP all
             benchmark processes within a minute (tenacity exhausts 5 attempts in ~75 s,
             so a router outage would otherwise turn in-flight tasks into error samples).
             Resume (SIGCONT) after 2 consecutive 200s.
Every 10 min: progress per run (judged/correct/running/stale), 429/503 counts scanned
             from task JSONs, Serper balance (freeze < 3000, resume > 10000), process
             liveness. Report appended to logs/gaia-val/monitor_sol_<TS>.log and a
             machine-readable logs/gaia-val/monitor_sol_<TS>.status.json.
Exits when every run has 165 judged tasks, or when all processes are gone.
"""
import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime

# Per-arm parameterization (env): PROBE_URL/PROBE_MODEL/PROBE_KEY_ENV pick the endpoint+model this
# monitor probes; ARM_MATCH filters benchmark processes to this arm (substring of cmdline, e.g. the
# config name "keendata-sol" / "keendata-kimi") so freezes never touch the other arm; LOAD_GATE=0
# skips the 6x60K concurrent recovery gate (used for stable commercial APIs to avoid token burn);
# MON_TAG names the log files when --dirs is used.
ROUTER = os.environ.get("PROBE_URL", "http://router.keendata.net:5343/v1/chat/completions")
PROBE_MODEL = os.environ.get("PROBE_MODEL", "gpt-5.6-sol")
PROBE_KEY_ENV = os.environ.get("PROBE_KEY_ENV", "OPENAI_API_KEY")
ARM_MATCH = os.environ.get("ARM_MATCH", "")
LOAD_GATE = os.environ.get("LOAD_GATE", "1") == "1"
MON_TAG = os.environ.get("MON_TAG", "")
RUNOUT_GLOB = os.environ.get("RUNOUT_GLOB", "")  # run.out path(s) — scanned for aux-role retry storms (hints/answer-type/extraction ride the router even on non-router arms)
N_TASKS = 165
STALE_MIN = 60
SERPER_FREEZE = 3000
SERPER_RESUME = 10000
ERR_FREEZE_THRESHOLD = 3   # >=3 LLM-call failures within 3 min across runs -> freeze
STABLE_STREAK = int(os.environ.get("STABLE_STREAK", "20"))  # probes (min) of consecutive health before resuming after a freeze (env-tunable)


def env_val(name):
    for line in open(os.path.expanduser("~/MiroFlow/.env")):
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def now():
    return datetime.now().strftime("%m-%d %H:%M:%S")


def probe_router(key):
    body = json.dumps({"model": PROBE_MODEL, "messages": [{"role": "user", "content": "Reply with exactly: OK"}], "max_completion_tokens": 32}).encode()
    req = urllib.request.Request(ROUTER, data=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001
        return 0


def load_probe(key, n=6, ntok=60000):
    """n concurrent ~ntok-token requests; returns number of non-200 (upstream TPM/concurrency capacity check)."""
    import concurrent.futures as cf
    unit = "The quick brown fox jumps over the lazy dog. "
    body = json.dumps({"model": PROBE_MODEL, "messages": [{"role": "user", "content": unit * (ntok // 10) + "\n\nIn one word, what animal jumps?"}], "max_completion_tokens": 400}).encode()
    def one(_):
        req = urllib.request.Request(ROUTER, data=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:  # noqa: BLE001
            return 0
    with cf.ThreadPoolExecutor(n) as ex:
        codes = list(ex.map(one, range(n)))
    return sum(1 for c in codes if c != 200), codes


def serper_balance(sk):
    if not sk:
        return None
    req = urllib.request.Request("https://google.serper.dev/account", headers={"X-API-KEY": sk})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("balance")
    except Exception:  # noqa: BLE001
        return None


def _arm_filter(pids):
    if not ARM_MATCH:
        return pids
    kept = []
    for p in pids:
        try:
            if ARM_MATCH in open(f"/proc/{p}/cmdline").read():
                kept.append(p)
        except Exception:  # noqa: BLE001
            pass
    return kept


def bench_pids():
    try:
        out = subprocess.run(["pgrep", "-f", "commo[n]-benchmark"], capture_output=True, text=True).stdout.split()
        return _arm_filter([int(p) for p in out])
    except Exception:  # noqa: BLE001
        return []


def worker_pids():
    """Only the python workers (main.py). Empirically, SIGSTOP sent to the whole bash/uv/python chain
    leaves python running (observed 8/17 twice); stopping the python children directly works."""
    try:
        out = subprocess.run(["pgrep", "-f", "python3 main.py commo[n]-benchmark"], capture_output=True, text=True).stdout.split()
        return _arm_filter([int(p) for p in out])
    except Exception:  # noqa: BLE001
        return []


def _stat(pid):
    try:
        return open(f"/proc/{pid}/stat").read().split(")")[1].split()[0]
    except Exception:  # noqa: BLE001
        return "?"


def signal_all(sig):
    """Send sig to python workers and verify the resulting state (T for STOP, non-T for CONT); retry up to 3x."""
    pids = worker_pids()
    want_stopped = sig == signal.SIGSTOP
    for _ in range(3):
        for p in pids:
            try:
                os.kill(p, sig)
            except ProcessLookupError:
                pass
        time.sleep(1)
        states = {p: _stat(p) for p in pids}
        ok = all((s == "T") == want_stopped for s in states.values() if s != "?")
        if ok:
            break
    return [f"{p}:{_stat(p)}" for p in pids]


ERR_STEP_SUFFIXES = ("_error",)
ERR_STEP_NAMES = ("sub_agent_llm_call_failed",)


def recent_llm_errors(dirs, window_s=180, mtime_s=900):
    """Count LLM-call failure steps (turn errors / llm_call_failed / summary errors) whose timestamp falls in the
    last `window_s` seconds, scanning only task JSONs modified within `mtime_s` (i.e. the in-flight ones).
    A partial 5xx storm shows up here long before (or without) the single probe seeing it."""
    now_t = time.time()
    cutoff = datetime.now() - __import__("datetime").timedelta(seconds=window_s)
    cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    n = 0
    for d in dirs:
        for f in glob.glob(os.path.join(d, "task_*_attempt_*.json")):
            try:
                if now_t - os.path.getmtime(f) > mtime_s:
                    continue
                j = json.load(open(f))
            except Exception:  # noqa: BLE001
                continue
            for s in j.get("step_logs", []):
                name = s.get("step_name", "")
                if (name.endswith(ERR_STEP_SUFFIXES) or name in ERR_STEP_NAMES) and s.get("timestamp", "") >= cutoff_s:
                    n += 1
    return n


def scan_dir(d):
    files = glob.glob(os.path.join(d, "task_*_attempt_*.json"))
    st = {"dir": os.path.basename(d), "files": len(files), "judged": 0, "correct": 0, "running": 0, "stale": [], "err429_files": 0, "err429": 0, "err503": 0, "errors": 0}
    t_now = time.time()
    for f in files:
        try:
            raw = open(f).read()
            j = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        jr = j.get("judge_result")
        if jr in ("CORRECT", "INCORRECT"):
            st["judged"] += 1
            st["correct"] += jr == "CORRECT"
        if j.get("status") != "completed":
            st["running"] += 1
            age = (t_now - os.path.getmtime(f)) / 60
            if age > STALE_MIN:
                st["stale"].append(f"{str(j.get('task_id'))[:8]}:{age:.0f}m")
        if j.get("error"):
            st["errors"] += 1
        n429 = raw.count("Error code: 429") + raw.count("RateLimitError")
        if n429:
            st["err429_files"] += 1
            st["err429"] += n429
        st["err503"] += raw.count("Service temporarily unavailable") + raw.count("Error code: 503")
    return st


def main():
    args = sys.argv[1:]
    once = "--once" in args
    if "--dirs" in args:
        i = args.index("--dirs")
        dirs = [a for a in args[i + 1:] if not a.startswith("--")]
        ts = MON_TAG or "manual"
    else:
        ts = args[args.index("--ts") + 1]
        dirs = sorted(glob.glob(os.path.expanduser(f"~/MiroFlow/logs/gaia-val/full165_e*_sol_{ts}")))
    if not dirs:
        print("no run dirs found")
        return 1
    log_path = os.path.expanduser(f"~/MiroFlow/logs/gaia-val/monitor_sol_{ts}.log")
    status_path = os.path.expanduser(f"~/MiroFlow/logs/gaia-val/monitor_sol_{ts}.status.json")
    key = env_val(PROBE_KEY_ENV)
    sk = env_val("SERPER_API_KEY")

    def log(msg):
        line = f"{now()} {msg}"
        print(line, flush=True)
        with open(log_path, "a") as fh:
            fh.write(line + "\n")

    log(f"monitor start dirs={[os.path.basename(d) for d in dirs]} once={once}")
    frozen = False
    reasons = set()
    prev_aux_fail = None
    good_streak = 0
    cycle = 0
    balance = serper_balance(sk)
    while True:
        cycle += 1
        code = probe_router(key)
        # 5xx / connection failure = upstream down; 403 = INSUFFICIENT_BALANCE (router balance exhausted).
        # Both turn every in-flight LLM call into an error within ~75 s -> freeze immediately.
        router_bad = code == 0 or code >= 500 or code in (402, 403)
        # Partial storms (some requests 5xx, probe mostly 200) are caught via fresh error steps in the traces.
        nerr = recent_llm_errors(dirs) if not frozen else 0
        if nerr >= ERR_FREEZE_THRESHOLD:
            router_bad = True
            log(f"trace error-rate: {nerr} LLM-call failures in last 3 min (BAD)")
        if RUNOUT_GLOB and not frozen:
            aux = 0
            for fpath in glob.glob(RUNOUT_GLOB):
                try:
                    aux += len(re.findall(r"Retry attempt 5 for (?:extract_hints|get_gaia_answer_type|extract_gaia_final_answer)", open(fpath, errors="ignore").read()))
                except Exception:  # noqa: BLE001
                    pass
            if prev_aux_fail is not None and aux - prev_aux_fail >= 3:
                router_bad = True
                log(f"aux-role failure surge: +{aux - prev_aux_fail} exhausted retries in run.out (BAD)")
            prev_aux_fail = aux
        if router_bad:
            good_streak = 0
            reasons.add("router")
            log(f"router probe sol={code} (BAD)")
        else:
            good_streak += 1
            # after a freeze, demand a longer clean streak (STABLE_STREAK probes) before resuming
            if "router" in reasons and good_streak >= STABLE_STREAK:
                nbad, codes = load_probe(key) if LOAD_GATE else (0, [])
                if nbad == 0:
                    reasons.discard("router")
                    log(f"router recovered (sol={code} x{good_streak}, load probe 6x60K all 200)")
                else:
                    good_streak = STABLE_STREAK - 3  # re-check in 3 min
                    log(f"load probe failed ({nbad}/6 non-200: {codes}) — staying frozen")

        if cycle % 10 == 1:
            balance = serper_balance(sk)
            if balance is not None:
                if balance < SERPER_FREEZE:
                    reasons.add("serper")
                elif balance > SERPER_RESUME:
                    reasons.discard("serper")

        if reasons and not frozen and not once:
            pids = signal_all(signal.SIGSTOP)
            frozen = True
            log(f"ALERT FREEZE reasons={sorted(reasons)} pids={pids}")
        elif frozen and not reasons and not once:
            pids = signal_all(signal.SIGCONT)
            frozen = False
            log(f"RESUME pids={pids}")

        if cycle % 10 == 1 or once:
            stats = [scan_dir(d) for d in dirs]
            pids = bench_pids()
            parts = []
            for s in stats:
                acc = f"{100 * s['correct'] / s['judged']:.1f}%" if s["judged"] else "-"
                parts.append(f"{s['dir'].split('_sol_')[0]}: judged {s['judged']}/{N_TASKS} correct {s['correct']} ({acc}) running {s['running']} stale {len(s['stale'])} 429 {s['err429']}({s['err429_files']}f) 503 {s['err503']} err {s['errors']}")
            log(f"PROGRESS | procs={len(pids)} frozen={frozen} serper={balance} router={code} || " + " || ".join(parts))
            for s in stats:
                if s["stale"]:
                    log(f"ALERT stale in {s['dir'].split('_sol_')[0]}: {s['stale']}")
            tot429 = sum(s["err429"] for s in stats)
            if tot429 >= 50:
                log(f"ALERT 429 total {tot429} — consider lowering concurrency next time")
            json.dump({"time": now(), "frozen": frozen, "reasons": sorted(reasons), "serper": balance, "router": code, "procs": len(pids), "runs": stats}, open(status_path, "w"), indent=1)
            done = all(s["judged"] >= N_TASKS for s in stats)
            if done or (not pids and not frozen and cycle > 5):
                log("FINAL " + " || ".join(parts) + (" (all judged)" if done else " (processes gone)"))
                return 0
        if once:
            return 0
        time.sleep(60)


if __name__ == "__main__":
    sys.exit(main())
