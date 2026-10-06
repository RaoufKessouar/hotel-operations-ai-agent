#!/usr/bin/env bash
set -euo pipefail

ROOT="${HOTEL_AGENT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
PYTHON="${HOTEL_AGENT_PYTHON:-python}"
MODEL_CACHE_DIR="${MODEL_CACHE_DIR:-$HOME/.cache/huggingface}"
MODEL_DEVICE="${MODEL_DEVICE:-cuda:0}"
SERVER="$ROOT/infra/llm/qwen_openai_server.py"
LOG="$ROOT/logs/qwen-server.log"
PID_FILE="$ROOT/logs/qwen-server.pid"

mkdir -p "$ROOT/logs"
if [[ -f "$PID_FILE" ]]; then
  pid="$(cat "$PID_FILE")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    command_line="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
    if [[ "$command_line" == *qwen_openai_server.py* ]]; then
      echo "Qwen est deja demarre (PID $pid)."
      exit 0
    fi
  fi
fi

if ss -ltn | grep -q ':8000 '; then
  echo "Le port 8000 est deja utilise par un autre processus." >&2
  exit 1
fi

nohup "$PYTHON" "$SERVER" \
  --host 127.0.0.1 \
  --port 8000 \
  --model Qwen/Qwen2.5-14B-Instruct \
  --device "$MODEL_DEVICE" \
  --cache-dir "$MODEL_CACHE_DIR" \
  --dtype float16 \
  --attention sdpa \
  > "$LOG" 2>&1 < /dev/null &
echo $! > "$PID_FILE"
echo "Chargement de Qwen demarre (PID $!)."
