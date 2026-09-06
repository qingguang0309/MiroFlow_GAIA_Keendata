#!/bin/bash
# f campaign = gpt-5.6-sol + Jina Reader (JINA_API_KEY present -> Jina becomes the primary scraper).
# Run ONLY after the e campaign has fully finished and the user has confirmed.
#   phase 0: preconditions — no benchmark processes; e dirs all 165 judged; JINA_API_KEY present in .env
#   phase 1: Jina probe from this server (r.jina.ai, same headers as MiroFlow) -> 200
#   phase 2: smoke 2 tasks (text L3 + png L2) -> check_smoke gate incl. Jina evidence ("URL Source:")
#   phase 3: fire f1/f2/f3 (165 each, max_concurrent=10) + monitor
# Usage: bash ~/MiroFlow/scripts_sol/fire_f_jina.sh [--skip-smoke] [--skip-precheck]
set -u
cd ~/MiroFlow || exit 1
UV=~/.local/bin/uv
LOG=logs/gaia-val/autofire_f.log
CFG=agent_gaia-validation-keendata-sol
SMOKE_WL="56db2318-640f-477a-a82f-bc93ad13e882,b7f857e4-d8aa-4387-af2a-0e844df5b9d8"
E_TS=$(cat logs/gaia-val/sol_campaign_ts.txt 2>/dev/null || echo 20260817_0255)
SKIP_SMOKE=0; SKIP_PRE=0
for a in "$@"; do [ "$a" = "--skip-smoke" ] && SKIP_SMOKE=1; [ "$a" = "--skip-precheck" ] && SKIP_PRE=1; done
log() { echo "$(date '+%F %T %Z') $*" | tee -a "$LOG"; }
KEY=$(grep -E "^OPENAI_API_KEY=" .env | cut -d= -f2- | tr -d '"')
JK=$(grep -E "^JINA_API_KEY=" .env | cut -d= -f2- | tr -d '"')

log "fire_f start (skip_smoke=$SKIP_SMOKE skip_precheck=$SKIP_PRE) e_ts=$E_TS"
[ -n "$JK" ] || { log "FATAL: JINA_API_KEY missing in .env"; exit 1; }
grep -q 'model_name: "gpt-5.6-sol"' config/$CFG.yaml || { log "FATAL: sol config missing"; exit 1; }
if [ $SKIP_PRE -eq 0 ]; then
  n=$(pgrep -fc "commo[n]-benchmark"); [ "$n" -eq 0 ] || { log "FATAL: $n benchmark processes still running (e not finished?)"; exit 1; }
  for r in 1 2 3; do
    D=logs/gaia-val/full165_e${r}_sol_$E_TS
    j=$(python3 - "$D" <<'EOF'
import json,glob,sys
n=0
for f in glob.glob(sys.argv[1]+"/task_*_attempt_*.json"):
    try: j=json.load(open(f))
    except Exception: continue
    n+= j.get("judge_result") in ("CORRECT","INCORRECT")
print(n)
EOF
)
    [ "$j" -ge 165 ] || { log "FATAL: e$r has only $j judged (<165) — e not finished"; exit 1; }
  done
  log "precheck ok: no procs, e1/e2/e3 all 165 judged"
fi

# ---- phase 1: Jina probe from AWS egress ----
code=$(curl -sS -m 90 -o /tmp/jina_probe.txt -D /tmp/jina_hdr.txt -w "%{http_code}" "https://r.jina.ai/https://example.com" \
  -H "Authorization: Bearer $JK" -H "X-Base: final" -H "X-Engine: browser" -H "X-With-Generated-Alt: true" 2>/dev/null)
rl=$(grep -i "x-ratelimit-limit" /tmp/jina_hdr.txt | tr -d '\r' | head -1); ut=$(grep -i "x-usage-tokens" /tmp/jina_hdr.txt | tr -d '\r' | head -1)
log "jina probe -> HTTP $code | $rl | $ut"
[ "$code" = "200" ] || { log "FATAL: Jina probe failed"; exit 1; }
c=$(curl -sS -m 60 -o /dev/null -w "%{http_code}" -X POST http://router.keendata.net:5343/v1/chat/completions -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -d '{"model":"gpt-5.6-sol","messages":[{"role":"user","content":"Reply with exactly: OK"}],"max_completion_tokens":32}')
log "router sol probe -> HTTP $c"; [ "$c" = "200" ] || { log "FATAL: router sol not 200 (balance?)"; exit 1; }

# ---- phase 2: smoke ----
if [ $SKIP_SMOKE -eq 0 ]; then
  SM=logs/gaia-val/smoke_soljina_$(date +%m%d_%H%M); mkdir -p "$SM"
  log "smoke start -> $SM"
  $UV run main.py common-benchmark --config_file_name=$CFG output_dir="$SM" "benchmark.data.whitelist=[$SMOKE_WL]" > "$SM/run.out" 2>&1
  log "smoke process exited rc=$?"
  python3 scripts_sol/check_smoke.py "$SM" --expect-model gpt-5.6-sol --min-tasks 2 --require-text "URL Source:" 2>&1 | tee -a "$LOG"
  rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then log "SMOKE FAILED rc=$rc — NOT firing f. Inspect $SM"; exit 2; fi
  log "SMOKE PASSED (Jina evidence present)"
fi

# ---- phase 3: fire f1/f2/f3 ----
TS=$(date +%Y%m%d_%H%M)
for r in 1 2 3; do
  D=logs/gaia-val/full165_f${r}_soljina_$TS; mkdir -p "$D"
  tmux new-session -d -s "f$r" "cd ~/MiroFlow && $UV run main.py common-benchmark --config_file_name=$CFG output_dir=$D benchmark.execution.max_concurrent=10 > $D/run.out 2>&1"
  log "FIRED f$r -> $D (tmux f$r)"
  sleep 20
done
sleep 30
tmux new-session -d -s mon "cd ~/MiroFlow && python3 scripts_sol/monitor_sol.py --dirs logs/gaia-val/full165_f1_soljina_$TS logs/gaia-val/full165_f2_soljina_$TS logs/gaia-val/full165_f3_soljina_$TS >> logs/gaia-val/monitor_f_${TS}.out 2>&1"
log "monitor started (tmux mon). fire_f done. ts=$TS"
echo "$TS" > logs/gaia-val/f_campaign_ts.txt
