#!/bin/bash
# f-plan v2 three-arm fire: f1 sol@keenrouter (fallback solor), f2 kimi@moonshot, f3 opus@openrouter.
# Aux roles (hint/answer-type/extraction/reasoning/VQA) all ride kimi-k3 via Moonshot.
# f3 full run auto-fires only when OpenRouter credits >= $150 (smoke runs regardless).
set -u
cd ~/MiroFlow || exit 1
UV=~/.local/bin/uv
LOG=logs/gaia-val/autofire_v2.log
K=$(grep -E "^OPENAI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
ORK=$(grep -E "^OPENROUTER_API_KEY=" .env | cut -d= -f2- | tr -d '"')
KK=$(grep -E "^KIMI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
SK=$(grep -E "^SERPER_API_KEY=" .env | cut -d= -f2- | tr -d '"')
SMOKE_WL="56db2318-640f-477a-a82f-bc93ad13e882,b7f857e4-d8aa-4387-af2a-0e844df5b9d8"
log(){ echo "$(date -u '+%F %T UTC') $*" | tee -a "$LOG"; }
python3 -c "import json; json.dump({'model':'gpt-5.6-sol','messages':[{'role':'user','content':'Reply with exactly: OK'}],'max_completion_tokens':32},open('/tmp/solprobe.json','w'))"
probe_sol(){ curl -sS -m 60 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $K" -H "Content-Type: application/json" -d @/tmp/solprobe.json 2>/dev/null; }
or_credits(){ curl -sS -m 20 https://openrouter.ai/api/v1/credits -H "Authorization: Bearer $ORK" | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(int(d['total_credits']-d['total_usage']))" 2>/dev/null || echo 0; }

log "fire_v2 start"
# prechecks
c=$(probe_sol); log "precheck sol=$c"; [ "$c" = "200" ] || { log "FATAL sol probe $c"; exit 1; }
c=$(curl -sS -m 30 -o /dev/null -w "%{http_code}" -X POST https://api.moonshot.cn/v1/chat/completions -H "Authorization: Bearer $KK" -H "Content-Type: application/json" -d '{"model":"kimi-k3","messages":[{"role":"user","content":"OK"}],"temperature":1,"max_tokens":8}'); log "precheck kimi=$c"; [ "$c" = "200" ] || { log "FATAL kimi probe $c"; exit 1; }
log "precheck serper=$(curl -sS -m 15 https://google.serper.dev/account -H "X-API-KEY: $SK" | python3 -c 'import sys,json;print(json.load(sys.stdin)["balance"])' 2>/dev/null)"
log "precheck openrouter_credits=\$$(or_credits)"

# sol stability gate: 20x200@60s + burst 8 + load 6x60K
STABLE_N=20
log "sol gate armed (${STABLE_N}x200 + burst8 + load6x60K)"
while true; do
  ok=0
  while [ $ok -lt $STABLE_N ]; do
    c=$(probe_sol); [ "$c" = "200" ] && ok=$((ok+1)) || ok=0
    log "gate sol=$c ok=$ok/$STABLE_N"; [ $ok -lt $STABLE_N ] && sleep 60
  done
  bad=0; for i in $(seq 1 8); do c=$(probe_sol); [ "$c" = "200" ] || bad=$((bad+1)); done
  log "gate burst $((8-bad))/8"
  if [ $bad -eq 0 ]; then
    python3 -c "import json; u='The quick brown fox jumps over the lazy dog. '; json.dump({'model':'gpt-5.6-sol','messages':[{'role':'user','content':u*6000+'In one word, what animal jumps?'}],'max_completion_tokens':400,'reasoning_effort':'low'},open('/tmp/loadbody.json','w'))"
    for i in $(seq 1 6); do (curl -sS -m 300 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $K" -H "Content-Type: application/json" -d @/tmp/loadbody.json > /tmp/lg_$i.txt 2>/dev/null) & done; wait
    lb=0; for i in $(seq 1 6); do [ "$(cat /tmp/lg_$i.txt)" = "200" ] || lb=$((lb+1)); done; rm -f /tmp/lg_*.txt
    log "gate load $((6-lb))/6"; [ $lb -eq 0 ] && break
  fi
  log "gate failed — recycling"; sleep 60
done
log "SOL GATE PASSED — smokes"

# parallel smokes (3 arms)
TSS=$(date +%m%d_%H%M)
S1=logs/gaia-val/smoke_v2sol_$TSS; S2=logs/gaia-val/smoke_v2kimi_$TSS; S3=logs/gaia-val/smoke_v2opus_$TSS
mkdir -p $S1 $S2 $S3
( AUX_LLM_CONCURRENCY=2 $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-sol  output_dir=$S1 "benchmark.data.whitelist=[$SMOKE_WL]" > $S1/run.out 2>&1 ) & P1=$!
( AUX_LLM_CONCURRENCY=2 FORCE_FIRST_TOOL_CALL=1 $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-kimi output_dir=$S2 "benchmark.data.whitelist=[$SMOKE_WL]" > $S2/run.out 2>&1 ) & P2=$!
( AUX_LLM_CONCURRENCY=2 $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-opus output_dir=$S3 "benchmark.data.whitelist=[$SMOKE_WL]" > $S3/run.out 2>&1 ) & P3=$!
log "smokes started: $S1 & $S2 & $S3"
wait $P1 $P2 $P3
log "smokes finished"
python3 scripts_sol/check_smoke.py $S1 --expect-model gpt-5.6-sol --min-tasks 2 --require-text "URL Source:" 2>&1 | tee -a "$LOG"; G1=${PIPESTATUS[0]}
python3 scripts_sol/check_smoke.py $S2 --expect-model "kimi-k3" --min-tasks 2 2>&1 | tee -a "$LOG"; G2=${PIPESTATUS[0]}
python3 scripts_sol/check_smoke.py $S3 --expect-model "anthropic/claude-opus-5,kimi-k3" --min-tasks 2 2>&1 | tee -a "$LOG"; G3=${PIPESTATUS[0]}
log "smoke gates: f1=$G1 f2=$G2 f3=$G3"
log "confidence-format spotcheck: $(grep -l "Confidence" $S1/task_*.json $S2/task_*.json $S3/task_*.json 2>/dev/null | wc -l)/6 files contain Confidence"

TS=$(date +%Y%m%d_%H%M)
# f1 full
if [ "$G1" -eq 0 ]; then
  D1=logs/gaia-val/full165_f1_sol_v2_$TS; mkdir -p $D1
  tmux new-session -d -s f1 "cd ~/MiroFlow && AUX_LLM_CONCURRENCY=2 $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-sol output_dir=$D1 benchmark.execution.max_concurrent=10 > $D1/run.out 2>&1"
  log "FIRED f1(sol) -> $D1"
  sleep 20
  tmux new-session -d -s mon_f1 "cd ~/MiroFlow && ARM_MATCH=keendata-sol MON_TAG=f1v2 LOAD_GATE=1 RUNOUT_GLOB=$D1/run.out python3 scripts_sol/monitor_sol.py --dirs $D1 >> logs/gaia-val/monitor_f1v2.out 2>&1"
else log "f1 SMOKE FAILED — sol arm NOT fired"; fi
# f2 full
if [ "$G2" -eq 0 ]; then
  D2=logs/gaia-val/full165_f2_kimi_v2_$TS; mkdir -p $D2
  tmux new-session -d -s f2 "cd ~/MiroFlow && AUX_LLM_CONCURRENCY=2 FORCE_FIRST_TOOL_CALL=1 $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-kimi output_dir=$D2 benchmark.execution.max_concurrent=5 > $D2/run.out 2>&1"
  log "FIRED f2(kimi) -> $D2"
  sleep 20
  tmux new-session -d -s mon_f2 "cd ~/MiroFlow && ARM_MATCH=keendata-kimi MON_TAG=f2v2 LOAD_GATE=0 PROBE_MODEL=kimi-k3 PROBE_URL=https://api.moonshot.cn/v1/chat/completions PROBE_KEY_ENV=KIMI_API_KEY RUNOUT_GLOB=$D2/run.out python3 scripts_sol/monitor_sol.py --dirs $D2 >> logs/gaia-val/monitor_f2v2.out 2>&1"
else log "f2 SMOKE FAILED — kimi arm NOT fired"; fi
# f3: gate on credits
if [ "$G3" -eq 0 ]; then
  CR=$(or_credits)
  if [ "$CR" -ge 150 ]; then
    D3=logs/gaia-val/full165_f3_opus_v2_$TS; mkdir -p $D3
    tmux new-session -d -s f3 "cd ~/MiroFlow && AUX_LLM_CONCURRENCY=2 $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-opus output_dir=$D3 benchmark.execution.max_concurrent=8 > $D3/run.out 2>&1"
    log "FIRED f3(opus) -> $D3"
    sleep 20
    tmux new-session -d -s mon_f3 "cd ~/MiroFlow && ARM_MATCH=keendata-opus MON_TAG=f3v2 LOAD_GATE=0 PROBE_MODEL=anthropic/claude-3-haiku PROBE_URL=https://openrouter.ai/api/v1/chat/completions PROBE_KEY_ENV=OPENROUTER_API_KEY RUNOUT_GLOB=$D3/run.out python3 scripts_sol/monitor_sol.py --dirs $D3 >> logs/gaia-val/monitor_f3v2.out 2>&1"
  else
    log "f3 SMOKE PASSED but credits \$$CR < \$150 — HOLDING full opus; credit-waiter armed"
    tmux new-session -d -s f3wait "bash ~/MiroFlow/scripts_sol/opus_credit_waiter.sh $TS >> ~/MiroFlow/logs/gaia-val/autofire_v2.out 2>&1"
  fi
else log "f3 SMOKE FAILED — opus arm NOT fired"; fi
log "fire_v2 done ts=$TS"
echo "$TS" > logs/gaia-val/v2_campaign_ts.txt
