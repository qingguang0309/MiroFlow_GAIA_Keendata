#!/bin/bash
# Waits for OpenRouter credits >= $150, then fires the full opus arm + its monitor.
set -u
cd ~/MiroFlow || exit 1
TS=${1:?ts}
UV=~/.local/bin/uv
LOG=logs/gaia-val/autofire_v2.log
ORK=$(grep -E "^OPENROUTER_API_KEY=" .env | cut -d= -f2- | tr -d '"')
log(){ echo "$(date -u '+%F %T UTC') $*" | tee -a "$LOG"; }
log "opus credit-waiter armed (fires at >=\$150; checks every 10 min)"
while true; do
  CR=$(curl -sS -m 20 https://openrouter.ai/api/v1/credits -H "Authorization: Bearer $ORK" | python3 -c "import sys,json;d=json.load(sys.stdin)['data'];print(int(d['total_credits']-d['total_usage']))" 2>/dev/null || echo 0)
  if [ "$CR" -ge 150 ]; then
    D3=logs/gaia-val/full165_f3_opus_v2_$TS; mkdir -p $D3
    tmux new-session -d -s f3 "cd ~/MiroFlow && AUX_LLM_CONCURRENCY=2 $UV run main.py common-benchmark --config_file_name=agent_gaia-validation-keendata-opus output_dir=$D3 benchmark.execution.max_concurrent=8 > $D3/run.out 2>&1"
    log "CREDITS \$$CR — FIRED f3(opus) -> $D3"
    sleep 20
    tmux new-session -d -s mon_f3 "cd ~/MiroFlow && ARM_MATCH=keendata-opus MON_TAG=f3v2 LOAD_GATE=0 PROBE_MODEL=anthropic/claude-3-haiku PROBE_URL=https://openrouter.ai/api/v1/chat/completions PROBE_KEY_ENV=OPENROUTER_API_KEY RUNOUT_GLOB=$D3/run.out python3 scripts_sol/monitor_sol.py --dirs $D3 >> logs/gaia-val/monitor_f3v2.out 2>&1"
    exit 0
  fi
  sleep 600
done
