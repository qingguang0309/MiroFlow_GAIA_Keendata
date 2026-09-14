#!/usr/bin/env python3
"""Pre-launch API preflight: call every external API a run will use, the way the run calls it.

Why (E35, 2026-09-13): the launch only curl-probed "the OpenRouter key" and "the Moonshot
key" separately. The pipeline combined them differently (tool servers read
KIMI_API_KEY + KIMI_BASE_URL), so the vision and reasoning tools failed with 401 for a
whole run while every standalone probe was green.

What it does, with the same environment as the launch (run it with the exact env
overrides the launch will use):
  1. Loads .env the way main.py does (existing environment wins), composes the Hydra
     config named by --config.
  2. Static lint of every key/base-URL pair: an OpenRouter key must only go to
     openrouter.ai and vice versa; no empty keys.
  3. Live probes: the main and sub-agent LLMs, each auxiliary role (hint, answer type,
     final answer), and one real tool call per MCP server, made through
     create_pipeline_components + ToolManager.execute_tool_call, i.e. the same server
     processes and env the agents get.
Any failure exits 1 unless explicitly waived with --waive PROBE=reason.

Usage:
  .venv/bin/python scripts_sol/preflight_api.py --config agent_gaia-validation-keendata-opus \
      [--override key=value ...] [--waive tool-audio:transcription=reason ...] [--skip-code-sandbox]
"""
import argparse
import asyncio
import base64
import os
import struct
import sys
import tempfile
import time
import wave
import zlib
from urllib.parse import urlparse

sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dotenv  # noqa: E402

dotenv.load_dotenv(os.path.join(os.getcwd(), ".env"))  # same precedence as main.py: process env wins

import hydra  # noqa: E402
from omegaconf import OmegaConf  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from api_error_signatures import classify  # noqa: E402
from config import config_path  # noqa: E402
from src.core.pipeline import create_pipeline_components  # noqa: E402
import src.utils.summary_utils as su  # noqa: E402

RESULTS = []  # (probe, status, detail)


def tail(key):
    return f"...{key[-4:]}" if key else "(empty)"


def record(probe, ok, detail):
    RESULTS.append((probe, "PASS" if ok else "FAIL", detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {probe:<34} {detail}", flush=True)


def lint_pair(name, key, base):
    host = urlparse(base or "").hostname or ""
    problems = []
    if not key:
        problems.append("empty API key")
    if key.startswith("sk-or-") and "openrouter.ai" not in host:
        problems.append(f"OpenRouter key sent to {host or base!r}")
    if "openrouter.ai" in host and key and not key.startswith("sk-or-"):
        problems.append("non-OpenRouter key sent to openrouter.ai")
    record(f"lint:{name}", not problems, f"key {tail(key)} -> {host or base}" + (f"  <-- {'; '.join(problems)}" if problems else ""))
    return not problems


def png_bytes(w=64, h=64, rgb=(220, 20, 20)):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def wav_file(path, seconds=1.5, rate=16000):
    import math
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(seconds * rate))))


async def llm_probe(name, key, base, model, gpt5=False):
    if not lint_pair(name, key, base):
        record(f"live:{name}", False, "skipped: lint failed")
        return
    t0 = time.time()
    try:
        client = AsyncOpenAI(api_key=key, base_url=base, timeout=120)
        kw = {"max_completion_tokens": 64} if gpt5 else {"max_tokens": 64}
        r = await client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Reply with exactly: OK"}], **kw)
        record(f"live:{name}", True, f"{model} @ {urlparse(base).hostname} ({time.time() - t0:.1f}s, finish={r.choices[0].finish_reason})")
    except Exception as e:  # noqa: BLE001
        record(f"live:{name}", False, f"{model} @ {urlparse(base).hostname}: {type(e).__name__}: {str(e)[:220]}")


def tool_env(tool_name):
    path = os.path.join(os.getcwd(), "config", "tool", f"{tool_name}.yaml")
    cfg = OmegaConf.load(path)
    return OmegaConf.to_container(cfg.get("env", {}), resolve=True) or {}


async def tool_probe(managers, server, tool, args, expect=None, name=None):
    name = name or f"{server}:{tool}"
    mgr = next((m for m in managers if m.get_server_params(server)), None)
    if mgr is None:
        return
    t0 = time.time()
    try:
        res = await mgr.execute_tool_call(server, tool, args)
    except Exception as e:  # noqa: BLE001
        record(name, False, f"exception {type(e).__name__}: {str(e)[:200]}")
        return None
    body = str(res.get("result") if isinstance(res, dict) and "result" in res else res)
    if isinstance(res, dict) and "error" in res:
        record(name, False, f"tool manager error: {str(res['error'])[:200]}")
        return None
    cat, blocking = classify(body, head_chars=4000)
    if cat and blocking:
        record(name, False, f"{cat}: {body[:220]!r}")
        return None
    if cat == "tool_error":
        record(name, False, f"tool reported an error: {body[:220]!r}")
        return None
    if expect and expect.lower() not in body.lower():
        record(name, False, f"missing expected {expect!r} in result: {body[:180]!r}")
        return None
    record(name, True, f"{time.time() - t0:.1f}s, {len(body)} chars")
    return body


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="agent config name, e.g. agent_gaia-validation-keendata-opus")
    ap.add_argument("--override", action="append", default=[], help="hydra override, same as on the launch command line")
    ap.add_argument("--waive", action="append", default=[], help="PROBE=reason: accept a known, explained failure")
    ap.add_argument("--skip-code-sandbox", action="store_true", help="do not create an E2B sandbox")
    a = ap.parse_args()
    waivers = dict(w.split("=", 1) for w in a.waive if "=" in w)

    with hydra.initialize_config_dir(config_dir=os.path.abspath(config_path()), version_base=None):
        cfg = hydra.compose(config_name=a.config, overrides=list(a.override))
    print(f"preflight_api: config={a.config} overrides={a.override or '-'} cwd={os.getcwd()}")

    # 1) main and sub-agent LLMs, using the fields each provider client reads
    llms = [("llm:main", cfg.main_agent.llm)] + [(f"llm:{k}", v.llm) for k, v in (cfg.get("sub_agents") or {}).items()]
    for name, llm in llms:
        pc = llm.provider_class
        if "OpenRouter" in pc:
            await llm_probe(name, llm.openrouter_api_key, llm.openrouter_base_url, llm.model_name)
        elif "OpenAI" in pc:
            await llm_probe(name, llm.openai_api_key, llm.openai_base_url, llm.model_name, gpt5="GPT5" in pc)
        else:
            record(name, False, f"no probe defined for provider_class {pc}")

    # 2) auxiliary roles: same key, base URLs and model-name resolution as summary_utils
    aux_key = cfg.main_agent.get("openai_api_key") or ""
    ip, op = cfg.main_agent.get("input_process") or {}, cfg.main_agent.get("output_process") or {}
    if ip.get("hint_generation"):
        await llm_probe("aux:hint", aux_key, ip.get("hint_llm_base_url"), su._hint_model())
    if op.get("final_answer_extraction"):
        base = op.get("final_answer_llm_base_url")
        await llm_probe("aux:answer_type", aux_key, base, su._answer_type_model())
        await llm_probe("aux:final_answer", aux_key, base, su._final_answer_model())

    # 3) tool servers: lint what each server will actually use, then one real call each
    main_mgr, sub_mgrs, _ = create_pipeline_components(cfg)
    managers = [main_mgr] + list(sub_mgrs.values())
    servers = sorted({s for m in managers for s in m.server_dict})
    print(f"  tool servers in this config: {servers}")
    tmp = tempfile.mkdtemp(prefix="preflight_")
    img = os.path.join(tmp, "red.png"); open(img, "wb").write(png_bytes())
    wav = os.path.join(tmp, "tone.wav"); wav_file(wav)

    known = {"tool-reasoning", "tool-image-video", "tool-audio", "tool-searching", "tool-reading", "tool-code"}
    for s in servers:
        if s not in known:
            record(f"{s}:*", False, "no functional probe defined for this server; add one or waive it")

    if "tool-reasoning" in servers:
        env = tool_env("tool-reasoning")
        if env.get("OPENAI_API_KEY"):
            lint_pair("tool-reasoning(openai-compatible)", env["OPENAI_API_KEY"], env.get("OPENAI_BASE_URL", ""))
        await tool_probe(managers, "tool-reasoning", "reasoning", {"question": "What is 17 multiplied by 23? Reply with the number only."}, expect="391")
    if "tool-image-video" in servers:
        env = tool_env("tool-image-video")
        if env.get("ANTHROPIC_API_KEY"):
            print("  note: tool-image-video uses Anthropic for VQA (ANTHROPIC_API_KEY is set)")
        elif env.get("OPENAI_API_KEY"):
            lint_pair("tool-image-video(openai-compatible)", env["OPENAI_API_KEY"], env.get("OPENAI_BASE_URL", ""))
        await tool_probe(managers, "tool-image-video", "visual_question_answering",
                         {"image_path_or_url": img, "question": "What single color fills this image? One word."}, expect="red")
        await tool_probe(managers, "tool-image-video", "visual_audio_youtube_analyzing",
                         {"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "question": "What animals appear in this video? A few words."})
    if "tool-audio" in servers:
        env = tool_env("tool-audio")
        lint_pair("tool-audio(openai-compatible)", env.get("OPENAI_API_KEY", ""), env.get("OPENAI_BASE_URL", ""))
        await tool_probe(managers, "tool-audio", "audio_transcription", {"audio_path_or_url": wav})
    if "tool-searching" in servers:
        await tool_probe(managers, "tool-searching", "google_search", {"q": "Wikipedia", "num": 1}, expect="wikipedia")
        await tool_probe(managers, "tool-searching", "scrape_website", {"url": "https://example.com"}, expect="Example Domain")
    if "tool-reading" in servers:
        await tool_probe(managers, "tool-reading", "read_file",
                         {"uri": "data:text/plain;base64," + base64.b64encode(b"preflight-reading-ok").decode()}, expect="preflight-reading-ok")
    if "tool-code" in servers:
        if a.skip_code_sandbox:
            record("tool-code:create_sandbox", False, "skipped by --skip-code-sandbox (waive it explicitly if intended)")
        else:
            body = await tool_probe(managers, "tool-code", "create_sandbox", {})
            import re
            sid = re.search(r"sandbox_id[\"'`:=\s]*([A-Za-z0-9_-]{8,})", body or "")
            if body is not None and not sid:
                record("tool-code:run_python_code", False, f"could not parse a sandbox id from: {body[:160]!r}")
            elif sid:
                await tool_probe(managers, "tool-code", "run_python_code", {"sandbox_id": sid.group(1), "code_block": "print(6*7)"}, expect="42")

    # verdict
    failures = [(p, d) for p, s, d in RESULTS if s == "FAIL"]
    unwaived = [(p, d) for p, d in failures if p not in waivers]
    print()
    for p, d in failures:
        if p in waivers:
            print(f"  WAIVED {p}: {waivers[p]}")
    unused = [w for w in waivers if w not in {p for p, _ in failures}]
    if unused:
        print(f"  note: waivers that matched no failure (drop them): {unused}")
    if unwaived:
        print(f"PREFLIGHT FAIL: {len(unwaived)} probe(s) failed. Do not launch; investigate first.")
        for p, d in unwaived:
            print(f"   - {p}: {d[:200]}")
        return 1
    print(f"PREFLIGHT PASS: {sum(1 for _, s, _ in RESULTS if s == 'PASS')} probes passed" + (f", {len(failures)} waived" if failures else ""))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
