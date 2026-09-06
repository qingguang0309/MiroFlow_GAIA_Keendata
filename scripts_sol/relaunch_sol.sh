#!/bin/bash
# Resume the sol 165x3 campaign after a router balance outage.
#   phase 0: wait until gpt-5.6-sol returns 200 on 2 consecutive probes 5 min apart (balance recharged)
#   phase 1: relaunch e1/e2/e3 into the SAME output dirs (MiroFlow resume: tasks with a judged boxed
#            answer are skipped; quarantined/missing tasks are re-run from scratch), then start monitor.
# Usage: bash ~/MiroFlow/scripts_sol/relaunch_sol.sh <TS>   (e.g. 20260817_0255)
set -u
cd ~/MiroFlow || exit 1
TS=${1:?TS required}
UV=~/.local/bin/uv
LOG=logs/gaia-val/autofire_sol.log
KEY=$(grep -E "^OPENAI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
CFG=agent_gaia-validation-keendata-sol
log() { echo "$(date '+%F %T %Z') $*" | tee -a "$LOG"; }
probe() {
  curl -sS -m 60 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"model":"gpt-5.6-sol","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_completion_tokens":32}' 2>/dev/null
}
for r in 1 2 3; do [ -d "logs/gaia-val/full165_e${r}_sol_$TS" ] || { log "FATAL: run dir e$r missing"; exit 1; }; done
# Stability gate: STABLE_N consecutive 200s at STABLE_GAP seconds (default 10 x 60 s = 10 min of health),
# then a burst of BURST_N rapid probes that must all be 200 (catches partial 5xx that a single probe misses).
STABLE_N=${STABLE_N:-10}; STABLE_GAP=${STABLE_GAP:-60}; BURST_N=${BURST_N:-8}
CONC=${CONC:-10}   # per-run max_concurrent (3 runs => 3*CONC to the router); lower it when the upstream is capacity-limited
log "relaunch armed for ts=$TS — waiting for router stability (${STABLE_N}x200 @${STABLE_GAP}s + burst ${BURST_N})"
while true; do
  ok=0
  while [ $ok -lt $STABLE_N ]; do
    c=$(probe); if [ "$c" = "200" ]; then ok=$((ok+1)); else ok=0; fi
    log "probe sol=$c consecutive_ok=$ok/$STABLE_N"
    [ $ok -lt $STABLE_N ] && sleep $STABLE_GAP
  done
  bad=0; for i in $(seq 1 $BURST_N); do c=$(probe); [ "$c" = "200" ] || bad=$((bad+1)); done
  log "burst probe: $((BURST_N-bad))/$BURST_N ok"
  if [ $bad -eq 0 ]; then
    # load gate: LOAD_N concurrent ~LOAD_TOK-token requests must all be 200 (upstream TPM/concurrency cap check)
    LOAD_N=${LOAD_N:-6}; LOAD_TOK=${LOAD_TOK:-60000}
    python3 -c "import json,sys; u='The quick brown fox jumps over the lazy dog. '; json.dump({'model':'gpt-5.6-sol','messages':[{'role':'user','content':u*($LOAD_TOK//10)+'\n\nIn one word, what animal jumps?'}],'max_completion_tokens':400,'reasoning_effort':'low'},open('/tmp/loadbody.json','w'))"
    for i in $(seq 1 $LOAD_N); do (curl -sS -m 300 -o /dev/null -w "%{http_code}\n" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @/tmp/loadbody.json > /tmp/loadres_$i.txt 2>/dev/null) & done; wait
    lbad=0; for i in $(seq 1 $LOAD_N); do [ "$(cat /tmp/loadres_$i.txt)" = "200" ] || lbad=$((lbad+1)); done; rm -f /tmp/loadres_*.txt
    log "load gate: $((LOAD_N-lbad))/$LOAD_N x ${LOAD_TOK}tok concurrent ok"
    [ $lbad -eq 0 ] && break
    log "load gate failed — upstream capacity still limited, restarting stability window"; sleep $STABLE_GAP
  else
    log "burst had failures — router still flapping, restarting stability window"; sleep $STABLE_GAP
  fi
done
log "ROUTER STABLE (${STABLE_N} consecutive 200 + burst clean) — RESUMING e1/e2/e3 (same dirs)"
for r in 1 2 3; do
  D=logs/gaia-val/full165_e${r}_sol_$TS
  tmux new-session -d -s "e$r" "cd ~/MiroFlow && $UV run main.py common-benchmark --config_file_name=$CFG output_dir=$D benchmark.execution.max_concurrent=$CONC >> $D/run.out 2>&1"
  log "RESUMED e$r -> $D (tmux e$r, max_concurrent=$CONC)"
  sleep 20
done
sleep 30
tmux new-session -d -s mon "cd ~/MiroFlow && python3 scripts_sol/monitor_sol.py --ts $TS >> logs/gaia-val/monitor_sol_${TS}.out 2>&1"
log "monitor restarted (tmux mon). relaunch done."
