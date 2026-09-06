#!/bin/bash
# f campaign: two arms in parallel, full 165 each, Jina primary scraping.
#   f1 = gpt-5.6-sol (keendata router)   config agent_gaia-validation-keendata-sol
#   f2 = kimi-k3     (Moonshot direct)   config agent_gaia-validation-keendata-kimi
# Flow: prechecks -> parallel 2-task smokes (both arms) -> gates -> fire f1+f2 -> per-arm monitors.
set -u
cd ~/MiroFlow || exit 1
UV=~/.local/bin/uv
LOG=logs/gaia-val/autofire_f.log
SMOKE_WL="56db2318-640f-477a-a82f-bc93ad13e882,b7f857e4-d8aa-4387-af2a-0e844df5b9d8"
KEY=$(grep -E "^OPENAI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
KIMI_KEY=$(grep -E "^KIMI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
JK=$(grep -E "^JINA_API_KEY=" .env | cut -d= -f2- | tr -d '"')
SK=$(grep -E "^SERPER_API_KEY=" .env | cut -d= -f2- | tr -d '"')
log() { echo "$(date '+%F %T %Z') $*" | tee -a "$LOG"; }

log "fire_f2 start"
[ -n "$KIMI_KEY" ] && [ -n "$JK" ] || { log "FATAL: KIMI/JINA key missing in .env"; exit 1; }
n=$(pgrep -fc "commo[n]-benchmark" || true); [ "${n:-0}" -eq 0 ] || { log "FATAL: $n benchmark processes already running"; exit 1; }

# --- prechecks ---
c=$(curl -sS -m 60 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"model":"gpt-5.6-sol","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_completion_tokens":32}')
log "precheck router sol -> $c"; [ "$c" = "200" ] || { log "FATAL: sol probe $c"; exit 1; }
c=$(curl -sS -m 60 -o /dev/null -w "%{http_code}" -X POST https://api.moonshot.cn/v1/chat/completions -H "Authorization: Bearer $KIMI_KEY" -H "Content-Type: application/json" -d '{"model":"kimi-k3","messages":[{"role":"user","content":"Reply with exactly: OK"}],"temperature":1,"max_tokens":16}')
log "precheck moonshot kimi-k3 -> $c"; [ "$c" = "200" ] || { log "FATAL: kimi probe $c"; exit 1; }
bal=$(curl -sS -m 20 "https://google.serper.dev/account" -H "X-API-KEY: $SK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('balance',0))" 2>/dev/null || echo 0)
log "precheck serper balance = $bal"; [ "${bal:-0}" -ge 15000 ] || { log "FATAL: serper < 15000"; exit 1; }
c=$(curl -sS -m 60 -o /dev/null -w "%{http_code}" "https://r.jina.ai/https://example.com" -H "Authorization: Bearer $JK" -H "X-Base: final" -H "X-Engine: browser")
log "precheck jina -> $c"; [ "$c" = "200" ] || { log "FATAL: jina probe $c"; exit 1; }

# --- sol stability gate (401/5xx storms recur on the router; smoke+fire only inside a proven-healthy window) ---
STABLE_N=${STABLE_N:-20}; STABLE_GAP=${STABLE_GAP:-60}; BURST_N=${BURST_N:-8}
probe_sol() { curl -sS -m 60 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @/tmp/solprobe.json 2>/dev/null; }
python3 -c "import json; json.dump({'model':'gpt-5.6-sol','messages':[{'role':'user','content':'Reply with exactly: OK'}],'max_completion_tokens':32},open('/tmp/solprobe.json','w'))"
log "sol stability gate armed (${STABLE_N}x200 @${STABLE_GAP}s + burst ${BURST_N} + load 6x60K)"
while true; do
  ok=0
  while [ $ok -lt $STABLE_N ]; do
    c=$(probe_sol); if [ "$c" = "200" ]; then ok=$((ok+1)); else ok=0; fi
    log "gate probe sol=$c ok=$ok/$STABLE_N"
    [ $ok -lt $STABLE_N ] && sleep $STABLE_GAP
  done
  bad=0; for i in $(seq 1 $BURST_N); do c=$(probe_sol); [ "$c" = "200" ] || bad=$((bad+1)); done
  log "gate burst: $((BURST_N-bad))/$BURST_N ok"
  if [ $bad -eq 0 ]; then
    python3 -c "import json; u='The quick brown fox jumps over the lazy dog. '; json.dump({'model':'gpt-5.6-sol','messages':[{'role':'user','content':u*6000+'In one word, what animal jumps?'}],'max_completion_tokens':400,'reasoning_effort':'low'},open('/tmp/loadbody.json','w'))"
    for i in $(seq 1 6); do (curl -sS -m 300 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d @/tmp/loadbody.json > /tmp/lr_$i.txt 2>/dev/null) & done; wait
    lbad=0; for i in $(seq 1 6); do [ "$(cat /tmp/lr_$i.txt)" = "200" ] || lbad=$((lbad+1)); done; rm -f /tmp/lr_*.txt
    log "gate load: $((6-lbad))/6 x 60K ok"
    [ $lbad -eq 0 ] && break
    log "gate load failed — restart stability window"; sleep $STABLE_GAP
  else
    log "gate burst failed — restart stability window"; sleep $STABLE_GAP
  fi
done
log "SOL GATE PASSED — proceeding to smokes"

# --- parallel smokes ---
TSS=$(date +%m%d_%H%M)
SM1=logs/gaia-val/smoke_fsol_$TSS; SM2=logs/gaia-val/smoke_fkimi_$TSS
mkdir -p "$SM1" "$SM2"
log "smoke start (parallel) -> $SM1 & $SM2"
$UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-sol  output_dir="$SM1" "benchmark.data.whitelist=[$SMOKE_WL]" > "$SM1/run.out" 2>&1 &
P1=$!
$UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-kimi output_dir="$SM2" "benchmark.data.whitelist=[$SMOKE_WL]" > "$SM2/run.out" 2>&1 &
P2=$!
wait $P1; R1=$?; wait $P2; R2=$?
log "smoke processes exited rc=$R1/$R2"
python3 scripts_sol/check_smoke.py "$SM1" --expect-model gpt-5.6-sol --min-tasks 2 --require-text "URL Source:" 2>&1 | tee -a "$LOG"; G1=${PIPESTATUS[0]}
python3 scripts_sol/check_smoke.py "$SM2" --expect-model "kimi-k3,gpt-5.6-sol" --min-tasks 2 2>&1 | tee -a "$LOG"; G2=${PIPESTATUS[0]}   # Jina evidence gate rides on f1; kimi may answer without scraping
[ "$G1" -eq 0 ] || log "SMOKE FAILED arm f1(sol) rc=$G1"
[ "$G2" -eq 0 ] || log "SMOKE FAILED arm f2(kimi) rc=$G2"
if [ "$G1" -ne 0 ] || [ "$G2" -ne 0 ]; then log "NOT firing (smoke gate). Inspect $SM1 / $SM2"; exit 2; fi
log "SMOKE PASSED both arms"

# --- fire ---
TS=$(date +%Y%m%d_%H%M)
D1=logs/gaia-val/full165_f1_sol_jina_$TS
D2=logs/gaia-val/full165_f2_kimi_jina_$TS
mkdir -p "$D1" "$D2"
tmux new-session -d -s f1 "cd ~/MiroFlow && $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-sol  output_dir=$D1 benchmark.execution.max_concurrent=10 > $D1/run.out 2>&1"
log "FIRED f1(sol)  -> $D1 (tmux f1)"
sleep 15
tmux new-session -d -s f2 "cd ~/MiroFlow && $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-kimi output_dir=$D2 benchmark.execution.max_concurrent=10 > $D2/run.out 2>&1"
log "FIRED f2(kimi) -> $D2 (tmux f2)"
sleep 30
tmux new-session -d -s mon_f1 "cd ~/MiroFlow && ARM_MATCH=keendata-sol  MON_TAG=f1 LOAD_GATE=1 RUNOUT_GLOB=$D1/run.out python3 scripts_sol/monitor_sol.py --dirs $D1 >> logs/gaia-val/monitor_f1.out 2>&1"
tmux new-session -d -s mon_f2 "cd ~/MiroFlow && ARM_MATCH=keendata-kimi MON_TAG=f2 LOAD_GATE=0 PROBE_MODEL=kimi-k3 PROBE_URL=https://api.moonshot.cn/v1/chat/completions PROBE_KEY_ENV=KIMI_API_KEY RUNOUT_GLOB=$D2/run.out python3 scripts_sol/monitor_sol.py --dirs $D2 >> logs/gaia-val/monitor_f2.out 2>&1"
log "monitors started (tmux mon_f1/mon_f2). fire_f2 done. ts=$TS"
echo "$TS" > logs/gaia-val/f_campaign_ts.txt
