#!/bin/bash
# f2 arm standalone: kimi-k3 full 165 (Moonshot direct), decoupled from the sol arm.
set -u
cd ~/MiroFlow || exit 1
UV=~/.local/bin/uv
LOG=logs/gaia-val/autofire_f.log
SMOKE_WL="56db2318-640f-477a-a82f-bc93ad13e882,b7f857e4-d8aa-4387-af2a-0e844df5b9d8"
KIMI_KEY=$(grep -E "^KIMI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
JK=$(grep -E "^JINA_API_KEY=" .env | cut -d= -f2- | tr -d '"')
SK=$(grep -E "^SERPER_API_KEY=" .env | cut -d= -f2- | tr -d '"')
log() { echo "$(date '+%F %T %Z') [kimi-arm] $*" | tee -a "$LOG"; }
log "fire_kimi start"
# Moonshot 429s are transient (per-minute TPM window) — wait them out instead of aborting.
for try in $(seq 1 30); do
  c=$(curl -sS -m 60 -o /dev/null -w "%{http_code}" -X POST https://api.moonshot.cn/v1/chat/completions -H "Authorization: Bearer $KIMI_KEY" -H "Content-Type: application/json" -d '{"model":"kimi-k3","messages":[{"role":"user","content":"Reply with exactly: OK"}],"temperature":1,"max_tokens":16}')
  log "precheck kimi -> $c (try $try)"
  [ "$c" = "200" ] && break
  [ "$try" = "30" ] && { log "FATAL: kimi probe still $c after 30 tries"; exit 1; }
  sleep 60
done
bal=$(curl -sS -m 20 "https://google.serper.dev/account" -H "X-API-KEY: $SK" | python3 -c "import sys,json; print(json.load(sys.stdin).get('balance',0))" 2>/dev/null || echo 0)
log "precheck serper = $bal"; [ "${bal:-0}" -ge 8000 ] || { log "FATAL: serper < 8000"; exit 1; }
if [ -z "${RESUME_DIR:-}" ]; then
  SM2=logs/gaia-val/smoke_fkimi_$(date +%m%d_%H%M); mkdir -p "$SM2"
  log "kimi smoke -> $SM2"
  $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-kimi output_dir="$SM2" "benchmark.data.whitelist=[$SMOKE_WL]" > "$SM2/run.out" 2>&1
  log "kimi smoke exited rc=$?"
  python3 scripts_sol/check_smoke.py "$SM2" --expect-model "kimi-k3,gpt-5.6-sol" --min-tasks 2 2>&1 | tee -a "$LOG"; G2=${PIPESTATUS[0]}
  if [ "$G2" -ne 0 ]; then log "SMOKE FAILED arm f2(kimi) rc=$G2 — NOT firing kimi"; exit 2; fi
  log "SMOKE PASSED f2(kimi)"
else
  log "resume mode: smoke already passed earlier — skipping"
fi
# Router mini-gate before firing: aux roles (hints/answer-type/extraction) ride the router,
# and firing launches 10 concurrent tasks whose hint calls all hit it at once. Wait for a
# clean window (10x200 @30s + burst 4) so the opening batch starts fully hinted.
ROUTER_KEY=$(grep -E "^OPENAI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
python3 -c "import json; json.dump({'model':'gpt-5.6-sol','messages':[{'role':'user','content':'Reply with exactly: OK'}],'max_completion_tokens':32},open('/tmp/solprobe_k.json','w'))"
probe_router() { curl -sS -m 45 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $ROUTER_KEY" -H "Content-Type: application/json" -d @/tmp/solprobe_k.json 2>/dev/null; }
while true; do
  ok=0
  while [ $ok -lt 10 ]; do c=$(probe_router); if [ "$c" = "200" ]; then ok=$((ok+1)); else log "router mini-gate probe=$c reset"; ok=0; fi; [ $ok -lt 10 ] && sleep 30; done
  bad=0; for i in 1 2 3 4; do c=$(probe_router); [ "$c" = "200" ] || bad=$((bad+1)); done
  [ $bad -eq 0 ] && break
  log "router mini-gate burst failed ($bad/4)"; sleep 30
done
log "ROUTER MINI-GATE PASSED — firing kimi"
if [ -n "${RESUME_DIR:-}" ]; then D2="$RESUME_DIR"; TS=$(basename "$D2" | grep -oE "[0-9]{8}_[0-9]{4}"); log "resuming into $D2"; else TS=$(date +%Y%m%d_%H%M); D2=logs/gaia-val/full165_f2_kimi_jina_$TS; mkdir -p "$D2"; fi
tmux new-session -d -s f2 "cd ~/MiroFlow && FORCE_FIRST_TOOL_CALL=1 $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-kimi output_dir=$D2 benchmark.execution.max_concurrent=${CONC_K:-6} > $D2/run.out 2>&1"
log "FIRED f2(kimi) -> $D2 (tmux f2)"
sleep 30
tmux new-session -d -s mon_f2 "cd ~/MiroFlow && ARM_MATCH=keendata-kimi MON_TAG=f2 LOAD_GATE=0 PROBE_MODEL=kimi-k3 PROBE_URL=https://api.moonshot.cn/v1/chat/completions PROBE_KEY_ENV=KIMI_API_KEY RUNOUT_GLOB=$D2/run.out python3 scripts_sol/monitor_sol.py --dirs $D2 >> logs/gaia-val/monitor_f2.out 2>&1"
log "monitor mon_f2 started. fire_kimi done. ts=$TS"
echo "$TS" > logs/gaia-val/f2_ts.txt
