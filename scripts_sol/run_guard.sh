#!/bin/bash
# In-run guard for a MiroFlow run started by launch_gated.sh.
#  1) API-anomaly tripwire: every 5 min scan task files touched since launch; any NEW
#     blocking API/tool error freezes the run (SIGSTOP) and writes FROZEN_API_ANOMALY.
#     Resume only by hand after investigating (E35: tool 401s hid in traces for a day).
#  2) OpenRouter balance floor: freeze below FLOOR, auto-resume above FLOOR+40, but never
#     resume while an API-anomaly freeze is in place.
# Usage: run_guard.sh <run_dir> <tmux_session> [floor=15] [start_epoch=now]
D=$1; SESS=$2; FLOOR=${3:-15}; T0=${4:-$(date +%s)}
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
OK=$(grep -E "^OPENROUTER_API_KEY=" .env | cut -d= -f2- | tr -d "\"' ")
log() { echo "$(date '+%F %T') $*" >> "$D/guard.log"; }
rem() {
  curl -sS -m 20 https://openrouter.ai/api/v1/credits -H "Authorization: Bearer $OK" 2>/dev/null |
    python3 -c "import sys,json; d=json.load(sys.stdin)['data']; print(round(d['total_credits']-d['total_usage'],2))" 2>/dev/null
}
freeze() { pkill -STOP -f "output_dir=$D " ; }
resume() { pkill -CONT -f "output_dir=$D " ; }

WAIVE_ARGS=()
if [ -f "$D/.trace_waivers" ]; then
  while IFS= read -r w; do [ -n "$w" ] && WAIVE_ARGS+=(--waive "$w"); done < "$D/.trace_waivers"
fi
log "guard start session=$SESS floor=$FLOOR since=$T0 trace_waivers=$(( ${#WAIVE_ARGS[@]} / 2 ))"
last_scan=0
while true; do
  tmux has-session -t "$SESS" 2>/dev/null || { log "run session ended, guard exit"; exit 0; }
  now=$(date +%s)
  if [ ! -f "$D/FROZEN_API_ANOMALY" ] && [ $((now - last_scan)) -ge 300 ]; then
    last_scan=$now
    # persistent errors (auth/quota/retired model) freeze on the first new instance; transient ones
    # (429/5xx/connection/rejected request) once they recur in >=3 tasks, since single transient
    # errors occur dozens of times in a normal 165-task run (f3).
    out=$($PY scripts_sol/scan_trace_errors.py "$D" --since "$T0" --state "$D/.trace_state.json" --transient-min-tasks 3 ${WAIVE_ARGS[@]+"${WAIVE_ARGS[@]}"} 2>&1); rc=$?
    if [ "$rc" = "1" ]; then
      freeze
      { echo "frozen at $(date '+%F %T')"; echo "$out"; echo; echo "after investigating: pkill -CONT -f 'output_dir=$D ' && rm $D/FROZEN_API_ANOMALY"; } > "$D/FROZEN_API_ANOMALY"
      log "FROZEN (API anomaly): $(echo "$out" | grep -E '^  BLOCK' | head -3 | tr -s ' ' | tr '\n' ';')"
    elif [ "$rc" != "0" ]; then
      log "tripwire scan error rc=$rc: $(echo "$out" | tail -2 | tr '\n' ' ')"
    fi
  fi
  r=$(rem)
  if [ -n "$r" ]; then
    if [ ! -f "$D/FROZEN_BALANCE" ] && python3 -c "import sys; sys.exit(0 if float('$r') < $FLOOR else 1)"; then
      freeze; touch "$D/FROZEN_BALANCE"; log "FROZEN (balance) remaining=$r; auto-resume above $((FLOOR + 40))"
    elif [ -f "$D/FROZEN_BALANCE" ] && python3 -c "import sys; sys.exit(0 if float('$r') > $((FLOOR + 40)) else 1)"; then
      rm -f "$D/FROZEN_BALANCE"; log "balance recovered remaining=$r"
      if [ -f "$D/FROZEN_API_ANOMALY" ]; then log "not resuming: API-anomaly freeze still in place"; else resume; log "RESUMED (SIGCONT)"; fi
    fi
  fi
  sleep 60
done
