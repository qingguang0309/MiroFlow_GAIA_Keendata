#!/bin/bash
# In-run guard for a MiroFlow run started by launch_gated.sh.
#  1) API-anomaly tripwire, every 5 min:
#     - tool results in task files touched since launch (scan_trace_errors.py), and
#     - failed main/sub-model calls (HTTP 401/402/403) in the per-task logs, which tool-result
#       scanning cannot see.
#     Any NEW blocking error freezes the run and writes FROZEN_API_ANOMALY. Resume only by hand
#     after investigating.
#  2) OpenRouter balance floor: freeze below FLOOR, auto-resume above FLOOR+40, but never while an
#     API-anomaly freeze is in place.
#
# How freezing works (E36, 2026-09-15). SIGSTOP must never hit the tmux pane's root process: tmux
# resumes a stopped pane root at once by sending SIGCONT to its whole process group. Earlier
# guards used pkill -STOP -f "output_dir=...", which also matched the pane's `bash -c` wrapper, so
# every freeze was silently undone and a test run spent OpenRouter to zero. This guard stops only
# the python run process, its descendants and the uv wrapper, then checks the kernel state. If the
# freeze does not hold it kills the run instead of letting it spend unguarded. A self-test right
# after start proves freezing works for this run.
#
# Usage: run_guard.sh <run_dir> <tmux_session> [floor=15] [start_epoch=now]
# Manual resume after an API-anomaly freeze has been investigated:
#   kill -CONT $(pgrep -f "output_dir=<run_dir>( |$)") && rm <run_dir>/FROZEN_API_ANOMALY
D=$1; SESS=$2; FLOOR=${3:-15}; T0=${4:-$(date +%s)}
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
OK=$(grep -E "^OPENROUTER_API_KEY=" .env | cut -d= -f2- | tr -d "\"' ")
LLM_ERR='LLM call failed: .*Error code: 40[123]|requires more credits'

log() { echo "$(date '+%F %T') $*" >> "$D/guard.log"; }
rem() {
  curl -sS -m 20 https://openrouter.ai/api/v1/credits -H "Authorization: Bearer $OK" 2>/dev/null |
    python3 -c "import sys,json; d=json.load(sys.stdin)['data']; print(round(d['total_credits']-d['total_usage'],2))" 2>/dev/null
}
# The run process is the one whose executable is python; shells or wrappers whose command line merely
# contains the same text (e.g. the pane's bash -c) must never be picked.
run_py() {
  local p
  for p in $(pgrep -f "main\.py common-benchmark.*output_dir=$D( |$)"); do
    case "$(cat "/proc/$p/comm" 2>/dev/null)" in python*) echo "$p"; return 0;; esac
  done
  return 1
}
descendants() { local c; for c in $(pgrep -P "$1"); do echo "$c"; descendants "$c"; done; }
# Never signal the tmux server or a tmux pane root (a direct child of the server). Stopping the
# server hangs every tmux session on this shared machine (happened once in testing, 2026-09-15);
# stopping a pane root is undone by tmux at once. If the python process is itself a pane root the
# freeze cannot be done safely: it is left out, the self-test fails, and the run is killed loudly.
safe_pids() {
  local ts p out=""
  ts=$(timeout 5 tmux display-message -p '#{pid}' 2>/dev/null)
  for p in "$@"; do
    [ -n "$p" ] && [ -d "/proc/$p" ] || continue
    case "$(cat "/proc/$p/comm" 2>/dev/null)" in tmux*) continue;; esac
    [ -n "$ts" ] && [ "$p" = "$ts" ] && continue
    [ -n "$ts" ] && [ "$(ps -o ppid= -p "$p" | tr -d ' ')" = "$ts" ] && continue
    out="$out $p"
  done
  echo $out
}
run_pids() {
  local py parent pids
  py=$(run_py); [ -n "$py" ] || return 1
  pids="$py $(descendants "$py" | tr '\n' ' ')"
  parent=$(ps -o ppid= -p "$py" | tr -d ' ')
  # the uv wrapper is safe to stop: it is a child of the pane root, not the pane root itself
  if [ -n "$parent" ] && tr '\0' ' ' < "/proc/$parent/cmdline" 2>/dev/null | grep -q "uv run"; then
    pids="$pids $parent"
  fi
  safe_pids $pids
}
freeze() {  # $1 = reason. 0 = frozen and verified, 1 = no run process, 2 = freeze failed and run killed
  local pids py st
  pids=$(run_pids) || { log "freeze ($1): run process not found"; return 1; }
  kill -STOP $pids 2>/dev/null
  sleep 3
  py=$(run_py)
  st=$(awk '/^State:/{print $2}' "/proc/$py/status" 2>/dev/null)
  if [ "$st" = "T" ]; then
    log "FROZEN ($1): python $py stopped; pids $pids"
    return 0
  fi
  log "FREEZE DID NOT HOLD ($1): python $py state=$st; killing the run so it cannot spend unguarded"
  kill -KILL $pids 2>/dev/null
  touch "$D/KILLED_BY_GUARD"
  return 2
}
resume() {  # $1 = reason
  local pids
  pids=$(run_pids) || { log "resume ($1): run process not found"; return 1; }
  kill -CONT $pids 2>/dev/null
  log "RESUMED ($1): pids $pids"
}
llm_err_count() { cat "$D"/task_logs/*.log 2>/dev/null | grep -cE "$LLM_ERR"; }

WAIVE_ARGS=()
if [ -f "$D/.trace_waivers" ]; then
  while IFS= read -r w; do [ -n "$w" ] && WAIVE_ARGS+=(--waive "$w"); done < "$D/.trace_waivers"
fi
log "guard start session=$SESS floor=$FLOOR since=$T0 trace_waivers=$(( ${#WAIVE_ARGS[@]} / 2 ))"

# self-test: prove that freezing holds for this run before relying on it
for _ in $(seq 1 30); do [ -n "$(run_py)" ] && break; sleep 2; done
if [ -n "$(run_py)" ]; then
  freeze "self-test"; st=$?
  if [ "$st" = "0" ]; then
    resume "self-test"
    log "self-test passed: freezing holds for this run"
  elif [ "$st" = "2" ]; then
    log "SELF-TEST FAILED: freezing does not hold; the run was killed. Investigate before relaunching."
    exit 3
  fi
else
  log "self-test skipped: run process not found within 60 s"
fi

LLM_BASE=$(llm_err_count)
last_scan=0
anomaly_was_set=0
while true; do
  tmux has-session -t "$SESS" 2>/dev/null || { log "run session ended, guard exit"; exit 0; }
  now=$(date +%s)
  if [ -f "$D/FROZEN_API_ANOMALY" ]; then
    anomaly_was_set=1
  else
    if [ "$anomaly_was_set" = "1" ]; then
      LLM_BASE=$(llm_err_count); anomaly_was_set=0
      log "API-anomaly marker removed by hand; model-call error baseline reset to $LLM_BASE"
    fi
    if [ $((now - last_scan)) -ge 300 ]; then
      last_scan=$now
      # persistent errors (auth/quota/retired model/tool backend) block on the first new instance;
      # transient ones once they recur in >=3 tasks, since single transient errors occur dozens of
      # times in a normal 165-task run (f3).
      out=$($PY scripts_sol/scan_trace_errors.py "$D" --since "$T0" --state "$D/.trace_state.json" --transient-min-tasks 3 ${WAIVE_ARGS[@]+"${WAIVE_ARGS[@]}"} 2>&1); rc=$?
      n=$(llm_err_count)
      if [ "$rc" = "1" ] || [ "$n" -gt "$LLM_BASE" ]; then
        {
          echo "frozen at $(date '+%F %T')"
          [ "$rc" = "1" ] && echo "$out"
          if [ "$n" -gt "$LLM_BASE" ]; then
            echo "failed model calls (401/402/403) in task logs: $LLM_BASE -> $n"
            cat "$D"/task_logs/*.log 2>/dev/null | grep -E "$LLM_ERR" | tail -3 | cut -c1-300
          fi
          echo
          echo "after investigating: kill -CONT \$(pgrep -f 'output_dir=$D( |\$)') && rm $D/FROZEN_API_ANOMALY"
        } > "$D/FROZEN_API_ANOMALY"
        log "API anomaly detected: $(echo "$out" | grep -E '^  BLOCK' | head -3 | tr -s ' ' | tr '\n' ';') model-call errors $LLM_BASE->$n"
        freeze "API anomaly"
        anomaly_was_set=1
      elif [ "$rc" != "0" ]; then
        log "tripwire scan error rc=$rc: $(echo "$out" | tail -2 | tr '\n' ' ')"
      fi
    fi
  fi
  r=$(rem)
  if [ -n "$r" ]; then
    if [ ! -f "$D/FROZEN_BALANCE" ] && python3 -c "import sys; sys.exit(0 if float('$r') < $FLOOR else 1)"; then
      touch "$D/FROZEN_BALANCE"
      freeze "balance $r below floor $FLOOR"
    elif [ -f "$D/FROZEN_BALANCE" ] && python3 -c "import sys; sys.exit(0 if float('$r') > $((FLOOR + 40)) else 1)"; then
      rm -f "$D/FROZEN_BALANCE"
      if [ -f "$D/FROZEN_API_ANOMALY" ]; then
        log "balance recovered to $r; not resuming: API-anomaly freeze still in place"
      else
        resume "balance recovered to $r"
      fi
    fi
  fi
  sleep 60
done
