#!/bin/bash
# Wrapper for launchd (templates in ops/launchd/). Not installed by the repository.
#
#   ops/run_tradingbot.sh service   # the runtime service (mock mode), runs until stopped
#   ops/run_tradingbot.sh watchdog  # one heartbeat check (launchd runs it every 60 s)
#
# Environment (set in the plist): TRADINGBOT_REPO (this checkout), TRADINGBOT_PYTHON (the venv
# interpreter), TRADINGBOT_RUNTIME_CONFIG (the runtime TOML), TRADINGBOT_LOG_DIR (log directory,
# outside cloud storage). Output goes through scripts/rotating_log.py (5 MB x 5 files per log).
set -euo pipefail

: "${TRADINGBOT_REPO:?set TRADINGBOT_REPO}"
: "${TRADINGBOT_PYTHON:?set TRADINGBOT_PYTHON}"
: "${TRADINGBOT_RUNTIME_CONFIG:?set TRADINGBOT_RUNTIME_CONFIG}"
LOG_DIR="${TRADINGBOT_LOG_DIR:-$HOME/Library/Logs/tradingbot}"
mkdir -p "$LOG_DIR"
cd "$TRADINGBOT_REPO"
export PYTHONPATH="$TRADINGBOT_REPO/src"

case "${1:-}" in
  service)
    # exec-like: SIGTERM from launchd reaches the service (clean stop, exit 0).
    "$TRADINGBOT_PYTHON" scripts/run_service.py --config "$TRADINGBOT_RUNTIME_CONFIG" 2>&1 \
      | "$TRADINGBOT_PYTHON" scripts/rotating_log.py "$LOG_DIR/service.log" &
    PIPE_PID=$!
    trap 'pkill -TERM -f "scripts/run_service.py --config $TRADINGBOT_RUNTIME_CONFIG" || true' TERM INT
    wait "$PIPE_PID"
    ;;
  watchdog)
    "$TRADINGBOT_PYTHON" scripts/watchdog.py --config "$TRADINGBOT_RUNTIME_CONFIG" 2>&1 \
      | "$TRADINGBOT_PYTHON" scripts/rotating_log.py "$LOG_DIR/watchdog.log" --max-bytes 1000000
    ;;
  *)
    echo "usage: $0 service|watchdog" >&2
    exit 2
    ;;
esac
