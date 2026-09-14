#!/bin/bash
# Gated launcher for MiroFlow GAIA runs. Lesson of E35 (2026-09-13): a partial env override
# sent the wrong key to two tools for a whole run and nothing stopped it.
# Rule: any API/tool anomaly in the preflight or the smoke traces stops the launch; during the
# run, run_guard.sh freezes on anomalies. Accepting a known condition needs an explicit
# waiver with a reason.
#
# Steps: code check -> preflight_api.py -> smoke run -> check_smoke.py + scan_trace_errors.py
#        -> fire + run_guard.sh (API-anomaly tripwire + OpenRouter balance floor)
# Preflight, smoke and the real run all execute inside tmux sessions with the same --env words,
# because tmux sessions take the tmux server's environment, not this shell's; a preflight run
# from the calling shell could see a different PATH or variables than the real run.
#
# Usage (runs inside the MiroFlow checkout):
#   scripts_sol/launch_gated.sh --config agent_gaia-validation-keendata-opus --out logs/gaia-val/<run_dir> \
#     [--concurrency 8] [--smoke-wl id1,id2] [--no-smoke] [--expect-model m1,m2] [--guard-floor 15] \
#     [--session NAME] [--env "KEY=VALUE KEY=VALUE"] [--override hydra.key=value]... \
#     [--waive PROBE=reason]... [--trace-waive SERVER:CATEGORY=reason]... [--dry-run]
#   --dry-run: code check + preflight only; print the smoke and run commands; fire nothing.
# Values in --env must not contain spaces.
set -u
cd "$(dirname "$0")/.." || exit 1
UV=${UV:-$HOME/.local/bin/uv}
PY=$PWD/.venv/bin/python
CONFIG=""; OUT=""; CONC=8; NO_SMOKE=0; FLOOR=15; SESSION=""; ENV_STR=""; DRY=0; EXPECT=""
SMOKE_WL="d8152ad6-e4d5-4c12-8bb7-8d57dc10c6de,dd3c7503-f62a-4bd0-9f67-1b63b94194cc"  # image+reasoning, reasoning+search (f3: correct, 0 tool errors)
PF_WAIVE=(); TR_WAIVE=(); OVR=()
while [ $# -gt 0 ]; do
  case "$1" in
    --config) CONFIG=$2; shift 2;;
    --out) OUT=${2%/}; shift 2;;
    --concurrency) CONC=$2; shift 2;;
    --smoke-wl) SMOKE_WL=$2; shift 2;;
    --no-smoke) NO_SMOKE=1; shift;;
    --expect-model) EXPECT=$2; shift 2;;
    --guard-floor) FLOOR=$2; shift 2;;
    --session) SESSION=$2; shift 2;;
    --env) ENV_STR=$2; shift 2;;
    --override) OVR+=("$2"); shift 2;;
    --waive) PF_WAIVE+=("$2"); shift 2;;
    --trace-waive) TR_WAIVE+=("$2"); shift 2;;
    --dry-run) DRY=1; shift;;
    *) echo "unknown argument: $1"; exit 2;;
  esac
done
[ -n "$CONFIG" ] && [ -n "$OUT" ] || { echo "need --config and --out"; exit 2; }
SESSION=${SESSION:-run_$(basename "$OUT" | tr -c 'A-Za-z0-9_\n' '_' | cut -c1-40)}
mkdir -p "$OUT"
LOG="$OUT/gate.log"
say() { echo "$(date '+%F %T') [gate] $*" | tee -a "$LOG"; }
mask() { local v=$1; [ ${#v} -gt 12 ] && echo "...${v: -4}" || echo "$v"; }
q() { local out=""; for x in "$@"; do out="$out $(printf '%q' "$x")"; done; echo "$out"; }

# Run a command in a detached tmux session (the environment the real run gets) and wait for it.
in_tmux() {  # session_name logfile command...
  local name=$1 logf=$2; shift 2
  local rcf="$logf.rc"; rm -f "$rcf"
  tmux has-session -t "$name" 2>/dev/null && { say "REFUSE: tmux session $name already exists"; return 97; }
  tmux new-session -d -s "$name" "cd $(printf '%q' "$PWD") &&$(q "$@") > $(printf '%q' "$logf") 2>&1; echo \$? > $(printf '%q' "$rcf")"
  while [ ! -f "$rcf" ]; do
    if ! tmux has-session -t "$name" 2>/dev/null; then sleep 2; [ -f "$rcf" ] || { say "tmux session $name ended without an exit code"; return 98; }; fi
    sleep 5
  done
  cat "$logf" | tee -a "$LOG"
  return "$(cat "$rcf")"
}

say "=== launch_gated config=$CONFIG out=$OUT session=$SESSION dry_run=$DRY"
ENV_WORDS=(); [ -n "$ENV_STR" ] && read -ra ENV_WORDS <<< "$ENV_STR"

# 0) environment hygiene
case " $ENV_STR " in
  *" KIMI_API_KEY="*) case " $ENV_STR " in *" KIMI_BASE_URL="*) ;; *) say "REFUSE: --env sets KIMI_API_KEY without KIMI_BASE_URL; tool-reasoning and tool-image-video read both (E35)"; exit 3;; esac;;
esac
for kv in ${ENV_WORDS[@]+"${ENV_WORDS[@]}"}; do say "env override ${kv%%=*}=$(mask "${kv#*=}")"; done
RISKY='^(KIMI_|HINT_LLM|FINAL_ANSWER_LLM|ANSWER_TYPE_LLM|OPENROUTER_|OPENAI_|GEMINI_|SERPER_|JINA_|E2B_)'
for kv in $(env | grep -E "$RISKY" | cut -d= -f1); do
  say "REFUSE: $kv is set in the calling shell and would silently override .env if tmux inherits it; unset it or pass it via --env"; exit 3
done
if tmux show-environment -g 2>/dev/null | grep -qE "$RISKY"; then
  say "REFUSE: the tmux server's global environment sets: $(tmux show-environment -g | grep -E "$RISKY" | cut -d= -f1 | tr '\n' ' ')- every tmux session would override .env with it"; exit 3
fi

# 1) code state
git pull -q --ff-only origin main || { say "STOP: git pull failed"; exit 4; }
say "HEAD $(git log -1 --oneline)"
dirty=$(git status --porcelain -- src config scripts_sol utils main.py common_benchmark.py)
[ -z "$dirty" ] || { say "STOP: tracked code has local modifications:"; echo "$dirty" | tee -a "$LOG"; exit 4; }

# 2) preflight: every API through the pipeline's own code path, inside tmux
PF=(scripts_sol/preflight_api.py --config "$CONFIG")
for o in ${OVR[@]+"${OVR[@]}"}; do PF+=(--override "$o"); done
for w in ${PF_WAIVE[@]+"${PF_WAIVE[@]}"}; do PF+=(--waive "$w"); done
say "preflight:$(q "${PF[@]}")"
in_tmux "${SESSION}_preflight" "$OUT/preflight.out" env ${ENV_WORDS[@]+"${ENV_WORDS[@]}"} "$PY" "${PF[@]}"; rc=$?
[ "$rc" = "0" ] || { say "STOP: preflight failed (rc=$rc). Investigate; waive only a failure you have explained."; exit 5; }

TR=(); for w in ${TR_WAIVE[@]+"${TR_WAIVE[@]}"}; do TR+=(--waive "$w"); done
if [ -z "$EXPECT" ]; then
  EXPECT=$("$PY" - "$CONFIG" <<'EOF'
import os, sys, hydra
sys.path.insert(0, os.getcwd())
from config import config_path
with hydra.initialize_config_dir(config_dir=os.path.abspath(config_path()), version_base=None):
    cfg = hydra.compose(config_name=sys.argv[1])
models = [cfg.main_agent.llm.model_name] + [v.llm.model_name for v in (cfg.get("sub_agents") or {}).values()]
print(",".join(dict.fromkeys(models)))
EOF
)
fi
RUN_BASE=(env ${ENV_WORDS[@]+"${ENV_WORDS[@]}"} "$UV" run main.py common-benchmark "--config_file_name=$CONFIG")

# 3) smoke: the real pipeline on a few tasks, inside tmux, then the strict trace gate
if [ "$NO_SMOKE" = "0" ]; then
  SM="${OUT}_smoke_$(date +%m%d_%H%M)"
  N=$(echo "$SMOKE_WL" | tr ',' '\n' | grep -c .)
  SMOKE=("${RUN_BASE[@]}" "output_dir=$SM" "benchmark.data.whitelist=[$SMOKE_WL]" "benchmark.execution.max_concurrent=$N" ${OVR[@]+"${OVR[@]}"})
  if [ "$DRY" = "1" ]; then
    say "dry-run: would smoke in tmux '${SESSION}_smoke':$(q "${SMOKE[@]}")"
  else
    mkdir -p "$SM"; say "smoke -> $SM ($N tasks)"
    in_tmux "${SESSION}_smoke" "$SM/run.out" "${SMOKE[@]}" > /dev/null; say "smoke exited rc=$?"
    "$PY" scripts_sol/check_smoke.py "$SM" --expect-model "$EXPECT" --min-tasks "$N" 2>&1 | tee -a "$LOG"; c1=${PIPESTATUS[0]}
    "$PY" scripts_sol/scan_trace_errors.py "$SM" ${TR[@]+"${TR[@]}"} 2>&1 | tee -a "$LOG"; c2=${PIPESTATUS[0]}
    if [ "$c1" != "0" ] || [ "$c2" != "0" ]; then say "STOP: smoke gate failed (check_smoke=$c1 trace_gate=$c2). Investigate before launching."; exit 6; fi
    say "smoke gate passed"
  fi
else
  say "WARNING: --no-smoke given; only the preflight gated this launch"
fi

# 4) fire + guard
RO="$OUT/run.out"; [ -e "$RO" ] && RO="$OUT/run_$(date +%m%d_%H%M).out"
RUN=("${RUN_BASE[@]}" "output_dir=$OUT" "benchmark.execution.max_concurrent=$CONC" ${OVR[@]+"${OVR[@]}"})
RUN_CMD="cd $(printf '%q' "$PWD") &&$(q "${RUN[@]}") > $(printf '%q' "$RO") 2>&1"
if [ "$DRY" = "1" ]; then
  say "dry-run: would run in tmux '$SESSION': $RUN_CMD"
  say "dry-run: would start guard '${SESSION}_guard' (floor=$FLOOR) with ${#TR_WAIVE[@]} trace waiver(s)"
  exit 0
fi
tmux has-session -t "$SESSION" 2>/dev/null && { say "REFUSE: tmux session $SESSION already exists"; exit 7; }
tmux has-session -t "${SESSION}_guard" 2>/dev/null && { say "REFUSE: tmux session ${SESSION}_guard already exists"; exit 7; }
: > "$OUT/.trace_waivers"; for w in ${TR_WAIVE[@]+"${TR_WAIVE[@]}"}; do echo "$w" >> "$OUT/.trace_waivers"; done
T0=$(date +%s)
tmux new-session -d -s "$SESSION" "$RUN_CMD"
tmux new-session -d -s "${SESSION}_guard" "bash scripts_sol/run_guard.sh $(printf '%q' "$OUT") $SESSION $FLOOR $T0"
say "FIRED session=$SESSION log=$RO guard=${SESSION}_guard (tripwire every 5 min, balance floor $FLOOR)"
say "watch: tail -f $OUT/guard.log ; an API-anomaly freeze writes $OUT/FROZEN_API_ANOMALY"
