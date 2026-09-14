"""Offline extractor A/B (M1, 2026-09-13).

Re-runs ONLY the final-answer extraction step (answer-type classifier + extractor
LLM + boxed parsing + GAIA judge) on the Agent Summaries stored in finished task
JSONs, so a rubric change can be validated on all 165 tasks without re-running
agents. Variants:
  --variant baseline   -> EXTRACTOR_ARBITRATION_PRIORS=0 (pre-M1 rubric)
  --variant new        -> EXTRACTOR_ARBITRATION_PRIORS=1 (M1 rubric)
The answer type per task is computed once and cached (--type-cache) so both
variants see the identical prompt template. Run from the MiroFlow root:

  .venv/bin/python scripts_sol/reextract_offline.py \
      logs/gaia-val/<run_dir> --variant new --out /tmp/new.jsonl --type-cache /tmp/types.json

Keys are read from MiroFlow/.env. kimi-k3 goes through the Moonshot official API by
project policy (2026-09-13):
Env: FINAL_ANSWER_LLM_MODEL_NAME / ANSWER_TYPE_LLM_MODEL_NAME (default kimi-k3),
EXTRACTOR_BASE_URL (default https://api.moonshot.cn/v1), EXTRACTOR_API_KEY (default
$KIMI_API_KEY). To route through OpenRouter instead, set EXTRACTOR_BASE_URL=
https://openrouter.ai/api/v1, EXTRACTOR_API_KEY=$OPENROUTER_API_KEY, both *_MODEL_NAME=
moonshotai/kimi-k3, and pass --rpm 16 (OpenRouter caps new accounts at 20 rpm for it).
"""
import argparse, asyncio, glob, json, os, re, sys, time
from collections import deque

sys.path.insert(0, os.getcwd())
import dotenv  # noqa: E402

# Load MiroFlow/.env before importing summary_utils (it reads AUX_LLM_CONCURRENCY at
# import time). Existing environment variables still take precedence.
dotenv.load_dotenv(os.path.join(os.getcwd(), ".env"))
import src.utils.summary_utils as su  # noqa: E402
from src.utils.io_utils import OutputFormatter  # noqa: E402
from utils.eval_utils import verify_answer_gaia  # noqa: E402

CONF = re.compile(r"\*\*Confidence:?\*\*:?\s*\[?(\d{1,3})\]?")


class RateBucket:
    """Sliding-window limiter on request STARTS. OpenRouter caps new accounts at
    20 rpm for moonshotai/kimi-k3; exceeding it returns 429, which tenacity then
    retries with 15s/30s/60s backoff — so overshooting the cap costs far more
    throughput than it buys. Gate every call, retries included."""

    def __init__(self, rpm):
        self.rpm = rpm; self.times = deque(); self.lock = asyncio.Lock()

    def penalize(self):
        """A 429 means the server's window is fuller than ours (e.g. requests from a
        just-killed run still count). Backdate the window to full so the next
        acquire waits instead of retrying straight into another 429."""
        now = time.monotonic()
        self.times = deque([now] * self.rpm)

    async def acquire(self):
        while True:
            async with self.lock:
                now = time.monotonic()
                while self.times and now - self.times[0] > 60:
                    self.times.popleft()
                if len(self.times) < self.rpm:
                    self.times.append(now); return
                wait = 60 - (now - self.times[0]) + 0.05
            await asyncio.sleep(wait)


def install_rate_limit(rpm):
    """Wrap summary_utils' AsyncOpenAI so every chat.completions.create — including
    tenacity retries inside the extractor — passes through the bucket."""
    bucket = RateBucket(rpm)
    orig = su.AsyncOpenAI

    class _Completions:
        def __init__(self, inner): self._inner = inner

        async def create(self, *args, **kwargs):
            # Absorb 429s here with a short sleep instead of letting them reach
            # the extractor's tenacity wrapper, whose 15s/30s/60s backoff (and
            # 5-attempt cap) turns a transient rate-limit into lost throughput
            # and spurious ERROR rows.
            for attempt in range(12):
                await bucket.acquire()
                try:
                    return await self._inner.chat.completions.create(*args, **kwargs)
                except Exception as e:
                    if getattr(e, "status_code", None) != 429 and "429" not in str(e)[:200]:
                        raise
                    bucket.penalize()
                    await asyncio.sleep(min(4 + attempt * 2, 20))
            raise RuntimeError("rate limited: gave up after 12 attempts")

    class _Chat:
        def __init__(self, inner): self.completions = _Completions(inner)

    class Limited:
        def __init__(self, *args, **kwargs):
            self._inner = orig(*args, **kwargs); self.chat = _Chat(self._inner)
        def __getattr__(self, name): return getattr(self._inner, name)

    su.AsyncOpenAI = Limited
    return bucket


def _text(c):
    return c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict))


def load_task(path):
    j = json.load(open(path, encoding="utf-8"))
    mh = j["main_agent_message_history"]["message_history"]
    if len(mh) < 2 or mh[-1]["role"] != "assistant" or not _text(mh[-1]["content"]).startswith("LLM extracted final answer"):
        return None
    if mh[-2]["role"] != "assistant":
        return None
    return dict(task_id=j["task_id"], question=j["input"]["task_description"], summary=_text(mh[-2]["content"]),
                gt=str(j.get("ground_truth") or ""), stored_ans=str(j.get("final_boxed_answer") or ""),
                stored_judge=j.get("judge_result"))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dirs", nargs="+"); ap.add_argument("--variant", choices=["baseline", "new"], required=True)
    ap.add_argument("--out", required=True); ap.add_argument("--type-cache", required=True)
    ap.add_argument("--concurrency", type=int, default=4); ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--rpm", type=int, default=60, help="global cap on request starts/min; use 16 when routing via OpenRouter (new-account cap 20 rpm for kimi-k3)")
    ap.add_argument("--only", default="", help="comma-separated task_id prefixes: restrict to these tasks (stability re-runs)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    os.environ["EXTRACTOR_ARBITRATION_PRIORS"] = "0" if a.variant == "baseline" else "1"
    # kimi-k3 goes through the Moonshot official API by project policy (2026-09-13).
    os.environ.setdefault("FINAL_ANSWER_LLM_MODEL_NAME", "kimi-k3")
    os.environ.setdefault("ANSWER_TYPE_LLM_MODEL_NAME", "kimi-k3")
    base_url = os.environ.get("EXTRACTOR_BASE_URL", "https://api.moonshot.cn/v1")
    api_key = os.environ.get("EXTRACTOR_API_KEY") or os.environ.get("KIMI_API_KEY", "")

    tasks, skipped = [], 0
    for d in a.run_dirs:
        for f in sorted(glob.glob(os.path.join(d, "task_*_attempt_1.json"))):
            t = load_task(f)
            if t: tasks.append(t)
            else: skipped += 1
    if a.only:
        pref = tuple(p.strip() for p in a.only.split(",") if p.strip())
        tasks = [t for t in tasks if t["task_id"].startswith(pref)]
    if a.limit: tasks = tasks[: a.limit]
    total = len(tasks)

    # Resume: keep rows already written for this variant, re-run only the missing
    # ones plus any that previously errored (extractor returning None after its 5
    # tenacity attempts shows up as judge=ERROR).
    prior_ok, prior_err = set(), set()
    if os.path.exists(a.out):
        kept = []
        for line in open(a.out, encoding="utf-8"):
            line = line.strip()
            if not line: continue
            r = json.loads(line)
            if r.get("judge") == "ERROR": prior_err.add(r["task_id"])
            else: prior_ok.add(r["task_id"]); kept.append(line)
        with open(a.out, "w", encoding="utf-8") as fh:  # drop ERROR rows, they get retried
            fh.write("\n".join(kept) + ("\n" if kept else ""))
        tasks = [t for t in tasks if t["task_id"] not in prior_ok]
    print(f"[reextract] variant={a.variant} todo={len(tasks)}/{total} (resume: done={len(prior_ok)} retry_err={len(prior_err)}) skipped={skipped} model={os.environ['FINAL_ANSWER_LLM_MODEL_NAME']} base={base_url}", file=sys.stderr)
    if a.dry_run:
        return
    assert api_key, "no API key (EXTRACTOR_API_KEY / KIMI_API_KEY)"
    install_rate_limit(a.rpm)
    print(f"[reextract] rate limit: {a.rpm} request starts/min, concurrency {a.concurrency}", file=sys.stderr)

    # answer-type cache: identical prompt template for both variants
    cache = json.load(open(a.type_cache)) if os.path.exists(a.type_cache) else {}
    orig_get_type = su.get_gaia_answer_type
    current = {}

    async def cached_get_type(task_description, key, url):
        tid = current.get(task_description)
        if tid and tid in cache:
            return cache[tid]
        r = await orig_get_type(task_description, key, url)
        if tid:
            cache[tid] = r
            json.dump(cache, open(a.type_cache, "w"), indent=0)
        return r
    su.get_gaia_answer_type = cached_get_type

    sem = asyncio.Semaphore(a.concurrency)
    out = open(a.out, "a", encoding="utf-8")
    done = {"n": 0, "correct": 0}

    async def one(t):
        current[t["question"]] = t["task_id"]
        async with sem:
            t0 = time.time()
            try:
                result = await su.extract_gaia_final_answer(t["question"], t["summary"], api_key, False, base_url)
                if result is None:
                    # tenacity's retry_error_callback swallows the final failure and
                    # returns None; surface it as a retryable ERROR row.
                    raise RuntimeError("extractor returned None after all retries")
                boxed = OutputFormatter()._extract_boxed_content(result)
                c = CONF.findall(result)
                judge = await verify_answer_gaia(t["gt"], boxed)
                rec = dict(task_id=t["task_id"], variant=a.variant, answer=boxed, conf=int(c[-1]) if c else None,
                           judge=judge, stored_answer=t["stored_ans"], stored_judge=t["stored_judge"], gt=t["gt"], secs=round(time.time() - t0, 1))
            except Exception as e:
                rec = dict(task_id=t["task_id"], variant=a.variant, answer=None, conf=None, judge="ERROR", error=str(e)[:200],
                           stored_answer=t["stored_ans"], stored_judge=t["stored_judge"], gt=t["gt"])
            out.write(json.dumps(rec, ensure_ascii=False) + "\n"); out.flush()
            done["n"] += 1; done["correct"] += rec["judge"] == "CORRECT"
            if done["n"] % 10 == 0:
                print(f"[reextract] {done['n']}/{len(tasks)} correct={done['correct']}", file=sys.stderr)

    await asyncio.gather(*(one(t) for t in tasks))
    print(f"[reextract] DONE variant={a.variant} correct={done['correct']}/{len(tasks)} -> {a.out}", file=sys.stderr)


asyncio.run(main())
