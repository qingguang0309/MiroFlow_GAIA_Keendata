#!/usr/bin/env python3
"""Build the full GAIA 2023 test split (301 tasks) in MiroFlow's standardized format.

Mirrors utils/prepare_benchmark/gen_gaia.py::gen_gaia_validation field for field:
  task_question = Question (unchanged), ground_truth = Final answer (masked "?" in test),
  file_path = the snapshot's relative "2023/test/<file>" or None, metadata = remaining columns
  (Level, Annotator Metadata).
Differences on purpose: it reads the already-downloaded Hugging Face snapshot
(data/GAIA/2023/test/metadata.parquet, fetched by GAIA_leaderboard/src/download_data.sh) instead
of re-downloading, and it copies the attachments to <out>/2023/test/ so the relative file_path
resolves against the new data_dir (an independent data_dir otherwise breaks attachment paths, E30).

Usage (from the MiroFlow root):
  .venv/bin/python scripts_sol/build_gaia_test_full.py [--snapshot data/GAIA] [--out data/gaia-test-full] [--force]
Refuses to write into a non-empty output folder unless --force.
"""
import argparse
import collections
import json
import os
import shutil
import sys

import pandas as pd


def plain(v):
    """numpy / pandas values -> JSON-serialisable Python values (recursively)."""
    if hasattr(v, "item") and not isinstance(v, (list, dict, str, bytes)):
        try:
            return v.item()
        except (ValueError, AttributeError):
            pass
    if hasattr(v, "tolist"):
        return v.tolist()
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", default="data/GAIA")
    ap.add_argument("--out", default="data/gaia-test-full")
    ap.add_argument("--level-as", choices=["keep", "str", "int"], default="keep",
                    help="cast metadata.Level to match the validation records")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    meta = os.path.join(a.snapshot, "2023", "test", "metadata.parquet")
    df = pd.read_parquet(meta)
    if os.path.isdir(a.out) and os.listdir(a.out) and not a.force:
        print(f"refusing: {a.out} exists and is not empty (use --force)")
        return 2
    os.makedirs(os.path.join(a.out, "2023", "test"), exist_ok=True)

    out_rows, copied = [], 0
    for rec in df.to_dict(orient="records"):
        rec = {k: plain(v) for k, v in rec.items()}
        task_id = rec.pop("task_id")
        question = rec.pop("Question")
        gt = rec.pop("Final answer")
        file_path = rec.pop("file_path") or ""
        rec.pop("file_name", None)
        if file_path:
            src = os.path.join(a.snapshot, file_path)
            if not os.path.exists(src):
                print(f"missing attachment for {task_id}: {file_path}")
                return 1
            dst = os.path.join(a.out, file_path)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
        if a.level_as == "str":
            rec["Level"] = str(rec["Level"])
        elif a.level_as == "int":
            rec["Level"] = int(rec["Level"])
        out_rows.append({"task_id": task_id, "task_question": question, "ground_truth": gt,
                         "file_path": file_path or None, "metadata": rec})

    path = os.path.join(a.out, "standardized_data.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for r in out_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    levels = collections.Counter(str(r["metadata"].get("Level")) for r in out_rows)
    ids = [r["task_id"] for r in out_rows]
    print(f"wrote {path}: {len(out_rows)} tasks | levels {dict(sorted(levels.items()))} | "
          f"attachments copied {copied} | duplicate ids {len(ids) - len(set(ids))} | "
          f"easter egg 0-0-0-0-0 present {'0-0-0-0-0' in ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
