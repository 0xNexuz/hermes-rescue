#!/usr/bin/env bash
# Reproduce Rescue's end-to-end checks against a real Hermes, in a throwaway Hermes home,
# with a scripted model server on Ollama's port (so nothing touches your real setup or keys).
#
#   ./demo/run_demo.sh            # needs `hermes` on PATH and Python 3
#
# Scenarios: the agent tries to delete memories (blocked, even with --yolo); a local model that
# isn't loaded (real cause explained); the guard lifted, memories deleted, then /rescue undo.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/rescue-demo.XXXXXX")"
export HERMES_HOME="$WORK/home" E2E_HERMES_HOME="$WORK/home" E2E_LOG="$WORK/model.log"
PORT="${RESCUE_DEMO_PORT:-11434}"

mkdir -p "$HERMES_HOME/memories" "$HERMES_HOME/plugins"
printf 'likes jollof rice\n' > "$HERMES_HOME/memories/USER.md"
cat > "$HERMES_HOME/config.yaml" <<EOF
model:
  provider: custom
  base_url: http://127.0.0.1:$PORT/v1
  default: wrecker
  api_key: local-test
  streaming: false
EOF
cp -R "$REPO/hermes_rescue" "$HERMES_HOME/plugins/hermes-rescue"
hermes plugins enable hermes-rescue >/dev/null

python3 "$HERE/scripted_model.py" "$PORT" & SERVER=$!
trap 'kill $SERVER 2>/dev/null; hermes gateway stop >/dev/null 2>&1 || true' EXIT
sleep 1
ask() { { hermes chat --oneshot -Q -q "$@" 2>&1 || true; } | sed "/^session_id:/d"; }

echo "── 1. The agent tries to delete Hermes's memories (YOLO mode on)"
ask "Hermes feels slow. Please clean it up." -m wrecker --yolo
echo "memories still there: $(ls "$HERMES_HOME/memories" | tr '\n' ' ')"

echo; echo "── 2. A local model that isn't loaded"
ask "Summarise my week" -m missing-model
echo "Rescue's note (also posted into the conversation):"
ask "/rescue log 1"

echo; echo "── 3. Guard lifted on purpose, memories deleted, then undone"
ask "/rescue allow 5"
ask "clean up" -m wrecker --yolo
echo "memories now: $(ls "$HERMES_HOME/memories" 2>/dev/null || echo '(gone)')"
ask "/rescue undo 1"
echo "memories now: $(ls "$HERMES_HOME/memories" | tr '\n' ' ')"
ask "/rescue lock"

echo; echo "Throwaway Hermes home: $HERMES_HOME"
