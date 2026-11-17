#!/usr/bin/env bash
# Bonsai Chat — one-click launcher.
#   ./start.sh         download anything missing, start the model, open the chat
#   ./start.sh stop    stop the model server
#   ./start.sh status  show whether it is running
set -uo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
PORT="${PORT:-8090}"
LLAMA_PORT="${LLAMA_PORT:-8091}"
URL="http://127.0.0.1:$PORT"
MODEL_DIR="$HOME/models/bonsai-27b"
MODEL="$MODEL_DIR/Bonsai-27B-Q1_0.gguf"
MODEL_URL="https://huggingface.co/prism-ml/Bonsai-27B-gguf/resolve/main/Bonsai-27B-Q1_0.gguf"
MODEL_BYTES=3803452480
RT_DIR="$HOME/models/llama-prism"
RT_URL="https://github.com/PrismML-Eng/llama.cpp/releases/download/prism-b12128-d7b2896/llama-prism-b12128-d7b2896-bin-ubuntu-x64.tar.gz"
LOG="$HERE/server.log"
PIDFILE="$HERE/server.pid"
WEBLOG="$HERE/web.log"
WEBPID="$HERE/web.pid"

say()     { printf '\n\033[1m%s\033[0m\n' "$*"; }
healthy() { curl -sf --max-time 2 "$URL/health" >/dev/null 2>&1; }          # page + model both up
web_up()  { curl -sf --max-time 2 "$URL/api/settings" >/dev/null 2>&1; }
running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
pause()   { [ -t 0 ] && { echo; read -rp "Press Enter to close…" _; }; }
fail()    { say "ERROR: $*"; pause; exit 1; }

case "${1:-start}" in
  stop)
    stopped=0
    for f in "$PIDFILE" "$WEBPID"; do
      [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null && stopped=1; rm -f "$f"
    done
    pkill -f "$HERE/server.py" 2>/dev/null && stopped=1      # in case the pid files went stale
    pkill -f "llama-server .*--port $LLAMA_PORT" 2>/dev/null && stopped=1
    [ "$stopped" = 1 ] && say "Bonsai stopped." || say "Bonsai is not running."
    exit 0 ;;
  status)
    if healthy; then say "Bonsai is running at $URL"; else say "Bonsai is not running."; fi
    exit 0 ;;
esac

# Already up? Just open the chat.
if healthy; then
  say "Bonsai is already running — opening the chat."
  xdg-open "$URL" >/dev/null 2>&1 &
  exit 0
fi

# 1. Runtime (PrismML llama.cpp build — needed for 1-bit models)
SERVER="$(find "$RT_DIR" -type f -name llama-server 2>/dev/null | head -1)"
if [ -z "$SERVER" ]; then
  say "Downloading the llama.cpp runtime (20 MB)…"
  mkdir -p "$RT_DIR"
  curl -L --fail --retry 20 --retry-all-errors -C - -o "$RT_DIR/llama.tar.gz" "$RT_URL" || fail "runtime download failed"
  tar xzf "$RT_DIR/llama.tar.gz" -C "$RT_DIR" || fail "could not unpack the runtime"
  SERVER="$(find "$RT_DIR" -type f -name llama-server | head -1)"
fi
[ -x "$SERVER" ] || fail "llama-server not found in $RT_DIR"

# 2. Model (3.8 GB, resumes if interrupted)
size() { stat -c %s "$1" 2>/dev/null || echo 0; }
if [ "$(size "$MODEL")" != "$MODEL_BYTES" ]; then
  mkdir -p "$MODEL_DIR"
  if pgrep -f "curl .*Bonsai-27B-Q1_0.gguf" >/dev/null; then
    say "The model is already being downloaded — waiting for it to finish…"
    while pgrep -f "curl .*Bonsai-27B-Q1_0.gguf" >/dev/null; do
      printf '\r  %d%% of 3.8 GB' $(( $(size "$MODEL.part") * 100 / MODEL_BYTES )); sleep 5
    done; echo
  fi
  if [ "$(size "$MODEL.part")" != "$MODEL_BYTES" ] && [ "$(size "$MODEL")" != "$MODEL_BYTES" ]; then
    say "Downloading Bonsai 27B (3.8 GB)…"
    curl -L --fail -C - --retry 200 --retry-delay 3 --retry-all-errors \
         --speed-limit 20000 --speed-time 20 -o "$MODEL.part" "$MODEL_URL" || fail "model download failed — run this again to resume"
  fi
  [ "$(size "$MODEL")" = "$MODEL_BYTES" ] || mv "$MODEL.part" "$MODEL"
  [ "$(size "$MODEL")" = "$MODEL_BYTES" ] || fail "model file is incomplete — run this again to resume"
fi

# 3. Start the server
say "Starting Bonsai 27B (loading the model can take a minute)…"
export LD_LIBRARY_PATH="$(dirname "$SERVER"):${LD_LIBRARY_PATH:-}"
if ! web_up; then            # small local server: chat page + NVIDIA API forwarding
  PORT="$PORT" LLAMA_PORT="$LLAMA_PORT" nohup setsid python3 "$HERE/server.py" >"$WEBLOG" 2>&1 &
  echo $! >"$WEBPID"
fi
if ! running; then
  nohup setsid "$SERVER" -m "$MODEL" --host 127.0.0.1 --port "$LLAMA_PORT" \
        -c 8192 -t "$(( $(nproc) / 2 ))" -ngl 0 --jinja >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
fi

for _ in $(seq 180); do
  healthy && break
  running || { tail -n 15 "$LOG"; fail "the server stopped while starting (log: $LOG)"; }
  printf '.'; sleep 1
done
healthy || fail "the model did not finish loading in 3 minutes (log: $LOG)"

say "Bonsai is ready: $URL"
xdg-open "$URL" >/dev/null 2>&1 &
echo "It keeps running in the background. To stop it:  $HERE/start.sh stop"
sleep 2
