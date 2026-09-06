#!/bin/bash
# f1 arm standalone: gpt-5.6-sol full 165 behind a stability gate; retries the whole
# gate+smoke cycle until a storm-free window lets the smoke pass, then fires.
set -u
cd ~/MiroFlow || exit 1
UV=~/.local/bin/uv
LOG=logs/gaia-val/autofire_f.log
SMOKE_WL="56db2318-640f-477a-a82f-bc93ad13e882,b7f857e4-d8aa-4387-af2a-0e844df5b9d8"
KEY=$(grep -E "^OPENAI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
STABLE_N=${STABLE_N:-20}; STABLE_GAP=${STABLE_GAP:-60}; BURST_N=${BURST_N:-8}
log() { echo "$(date '+%F %T %Z') [sol-arm] $*" | tee -a "$LOG"; }
python3 -c "import json; json.dump({'model':'gpt-5.6-sol','messages':[{'role':'user','content':'Reply with exactly: OK'}],'max_completion_tokens':32},open('/tmp/solprobe.json','w'))"
probe_sol() { curl -sS -m 60 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @/tmp/solprobe.json 2>/dev/null; }
log "fire_sol armed (gate ${STABLE_N}x200 + burst ${BURST_N} + load 6x60K; retries whole cycle after storm-poisoned smokes)"
round=0
while true; do
  round=$((round+1))
  ok=0
  while [ $ok -lt $STABLE_N ]; do
    c=$(probe_sol); if [ "$c" = "200" ]; then ok=$((ok+1)); else ok=0; fi
    [ $((ok % 5)) -eq 0 ] || [ "$c" != "200" ] && log "gate r$round probe sol=$c ok=$ok/$STABLE_N"
    [ $ok -lt $STABLE_N ] && sleep $STABLE_GAP
  done
  bad=0; for i in $(seq 1 $BURST_N); do c=$(probe_sol); [ "$c" = "200" ] || bad=$((bad+1)); done
  [ $bad -eq 0 ] || { log "gate r$round burst failed ($bad/$BURST_N bad)"; sleep $STABLE_GAP; continue; }
  python3 -c "import json; u='The quick brown fox jumps over the lazy dog. '; json.dump({'model':'gpt-5.6-sol','messages':[{'role':'user','content':u*6000+'In one word, what animal jumps?'}],'max_completion_tokens':400,'reasoning_effort':'low'},open('/tmp/loadbody.json','w'))"
  for i in $(seq 1 6); do (curl -sS -m 300 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @/tmp/loadbody.json > /tmp/lr_$i.txt 2>/dev/null) & done; wait
  lbad=0; for i in $(seq 1 6); do [ "$(cat /tmp/lr_$i.txt)" = "200" ] || lbad=$((lbad+1)); done; rm -f /tmp/lr_*.txt
  [ $lbad -eq 0 ] || { log "gate r$round load failed ($lbad/6)"; sleep $STABLE_GAP; continue; }
  log "SOL GATE PASSED r$round — smoke"
  SM1=logs/gaia-val/smoke_fsol_$(date +%m%d_%H%M); mkdir -p "$SM1"
  $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-sol output_dir="$SM1" "benchmark.data.whitelist=[$SMOKE_WL]" > "$SM1/run.out" 2>&1
  python3 scripts_sol/check_smoke.py "$SM1" --expect-model gpt-5.6-sol --min-tasks 2 --require-text "URL Source:" 2>&1 | tee -a "$LOG"; G1=${PIPESTATUS[0]}
  if [ "$G1" -ne 0 ]; then log "SMOKE FAILED r$round (likely a storm window) — recycling gate"; sleep $STABLE_GAP; continue; fi
  log "SMOKE PASSED f1(sol) r$round"
  break
done
TS=$(date +%Y%m%d_%H%M)
D1=logs/gaia-val/full165_f1_sol_jina_$TS; mkdir -p "$D1"
tmux new-session -d -s f1 "cd ~/MiroFlow && $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-sol output_dir=$D1 benchmark.execution.max_concurrent=10 > $D1/run.out 2>&1"
log "FIRED f1(sol) -> $D1 (tmux f1)"
sleep 30
tmux new-session -d -s mon_f1 "cd ~/MiroFlow && ARM_MATCH=keendata-sol MON_TAG=f1 LOAD_GATE=1 RUNOUT_GLOB=$D1/run.out python3 scripts_sol/monitor_sol.py --dirs $D1 >> logs/gaia-val/monitor_f1.out 2>&1"
log "monitor mon_f1 started. fire_sol done. ts=$TS"
echo "$TS" > logs/gaia-val/f1_ts.txt
