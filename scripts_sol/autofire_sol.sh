#!/bin/bash
# Autofire for the gpt-5.6-sol 165x3 campaign (runs inside tmux on the AWS server).
#   phase 0: wait until router returns 200 for gpt-5.6-sol on 2 consecutive probes 5 min apart
#   phase 1: smoke 2 tasks (text L3 + png L2) with the sol config; gate via check_smoke.py
#   phase 3: fire e1/e2/e3 (165 tasks each, max_concurrent=10) in tmux, then start monitor_sol.py
# Usage: bash ~/MiroFlow/scripts_sol/autofire_sol.sh [--skip-wait] [--skip-smoke]
set -u
cd ~/MiroFlow || exit 1
UV=~/.local/bin/uv
LOG=logs/gaia-val/autofire_sol.log
KEY=$(grep -E "^OPENAI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
CFG=agent_gaia-validation-keendata-sol
SMOKE_WL="56db2318-640f-477a-a82f-bc93ad13e882,b7f857e4-d8aa-4387-af2a-0e844df5b9d8"
SKIP_WAIT=0; SKIP_SMOKE=0
for a in "$@"; do [ "$a" = "--skip-wait" ] && SKIP_WAIT=1; [ "$a" = "--skip-smoke" ] && SKIP_SMOKE=1; done

log() { echo "$(date '+%F %T %Z') $*" | tee -a "$LOG"; }
probe() {
  curl -sS -m 60 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"model":"gpt-5.6-sol","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_completion_tokens":32}' 2>/dev/null
}

log "autofire start (skip_wait=$SKIP_WAIT skip_smoke=$SKIP_SMOKE) cfg=$CFG"
grep -q 'model_name: "gpt-5.6-sol"' config/$CFG.yaml || { log "FATAL: sol config missing"; exit 1; }
[ "$(grep -c '=gpt-5.6-sol$' .env)" -ge 5 ] || { log "FATAL: .env model vars not switched to sol"; exit 1; }

# ---- phase 0: wait for router ----
if [ $SKIP_WAIT -eq 0 ]; then
  ok=0
  while [ $ok -lt 2 ]; do
    c=$(probe)
    if [ "$c" = "200" ]; then ok=$((ok+1)); else ok=0; fi
    log "probe sol=$c consecutive_ok=$ok"
    [ $ok -lt 2 ] && sleep 300
  done
  log "SOL RECOVERED (2 consecutive 200, 5 min apart)"
fi

# ---- phase 1: smoke ----
if [ $SKIP_SMOKE -eq 0 ]; then
  SM=logs/gaia-val/smoke_sol_$(date +%m%d_%H%M); mkdir -p "$SM"
  log "smoke start -> $SM"
  $UV run main.py common-benchmark --config_file_name=$CFG output_dir="$SM" "benchmark.data.whitelist=[$SMOKE_WL]" > "$SM/run.out" 2>&1
  log "smoke process exited rc=$?"
  python3 scripts_sol/check_smoke.py "$SM" --expect-model gpt-5.6-sol --min-tasks 2 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then log "SMOKE FAILED rc=$rc — NOT firing full runs. Inspect $SM"; exit 2; fi
  log "SMOKE PASSED"
fi

# ---- phase 3: fire e1/e2/e3 ----
c=$(probe); [ "$c" = "200" ] || { log "router sol=$c right before firing — abort"; exit 3; }
TS=$(date +%Y%m%d_%H%M)
for r in 1 2 3; do
  D=logs/gaia-val/full165_e${r}_sol_$TS; mkdir -p "$D"
  tmux new-session -d -s "e$r" "cd ~/MiroFlow && $UV run main.py common-benchmark --config_file_name=$CFG output_dir=$D benchmark.execution.max_concurrent=10 > $D/run.out 2>&1"
  log "FIRED e$r -> $D (tmux e$r)"
  sleep 20
done
sleep 30
tmux new-session -d -s mon "cd ~/MiroFlow && python3 scripts_sol/monitor_sol.py --ts $TS >> logs/gaia-val/monitor_sol_${TS}.out 2>&1"
log "monitor started (tmux mon, ts=$TS). autofire done."
echo "$TS" > logs/gaia-val/sol_campaign_ts.txt
