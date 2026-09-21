#!/bin/bash
# Resume a run that run_guard.sh froze, and remove the freeze marker.
#
# Why this exists (E39, 2026-09-20): the guard freezes the python process AND every descendant,
# which includes the per-call MCP server subprocesses. The instruction it used to print,
#   kill -CONT $(pgrep -f "output_dir=<run_dir>( |$)")
# only matches processes whose command line carries output_dir=, which the MCP children do not. So a
# manual resume left them stopped: every in-flight tool call then hung until its 900s deadline and
# timed out at once, which looks exactly like "resuming causes a burst of timeouts" and re-trips the
# tripwire. That is very likely what happened on 2026-09-18 as well.
#
# Usage: bash scripts_sol/resume_run.sh <run_dir>
# Run it from the MiroFlow root. It refuses to signal the tmux server or a pane root, exactly like
# the guard does, and reports anything still stopped afterwards.
set -u
D=${1:?usage: resume_run.sh <run_dir>}
D=${D%/}

run_py() {
  local p
  for p in $(pgrep -f "main\.py common-benchmark.*output_dir=$D( |$)"); do
    case "$(cat "/proc/$p/comm" 2>/dev/null)" in python*) echo "$p"; return 0;; esac
  done
  return 1
}
descendants() { local c; for c in $(pgrep -P "$1"); do echo "$c"; descendants "$c"; done; }
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

py=$(run_py) || { echo "resume: no run process for output_dir=$D"; exit 1; }
parent=$(ps -o ppid= -p "$py" | tr -d ' ')
pids="$py $(descendants "$py" | tr '\n' ' ')"
if [ -n "$parent" ] && tr '\0' ' ' < "/proc/$parent/cmdline" 2>/dev/null | grep -q "uv run"; then
  pids="$pids $parent"
fi
pids=$(safe_pids $pids)
kill -CONT $pids 2>/dev/null
sleep 3
rm -f "$D/FROZEN_API_ANOMALY"
echo "resumed pids:$pids"
echo "freeze marker removed"

left=$(for p in $pids; do
  [ -d "/proc/$p" ] || continue
  [ "$(awk '/^State:/{print $2}' "/proc/$p/status" 2>/dev/null)" = "T" ] && echo "$p"
done)
if [ -n "$left" ]; then echo "STILL STOPPED: $left"; exit 2; fi

# Orphaned MCP servers from an earlier killed run leak and stay stopped; report them, do not touch.
orph=$(ps -eo pid,ppid,stat,args --no-headers | awk '$3 ~ /^T/ && $2 == 1 && /mcp_servers/ {print $1}')
[ -n "$orph" ] && echo "note: stopped orphaned MCP servers from an earlier run (safe to kill -9): $orph"
echo "resume complete; nothing left stopped in this run"
