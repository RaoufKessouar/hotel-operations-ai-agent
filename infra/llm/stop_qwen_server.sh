#!/usr/bin/env bash
set -euo pipefail

ROOT="${HOTEL_AGENT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PID_FILE="$ROOT/logs/qwen-server.pid"
if [[ ! -f "$PID_FILE" ]]; then
  echo "Aucun PID Qwen enregistre."
  exit 0
fi
pid="$(cat "$PID_FILE")"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Le service Qwen est deja arrete."
  exit 0
fi
command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
if [[ "$command_line" != *qwen_openai_server.py* ]]; then
  echo "Refus : le PID ne correspond pas au service Qwen attendu." >&2
  exit 1
fi
kill -TERM "$pid"
for _ in {1..30}; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Service Qwen arrete."
    exit 0
  fi
  sleep 1
done
echo "Le service Qwen ne s'est pas arrete proprement." >&2
exit 1
