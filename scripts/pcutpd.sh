#!/usr/bin/env bash
set -euo pipefail

# Start PCUTP in a detached screen without depending on the current directory.
# Settings can be overridden with environment variables shown by `help`.
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_dir=$(cd -- "$script_dir/.." && pwd)

session=${PCUTP_SESSION:-pcutpd}
port=${PCUTP_PORT:-/dev/ttyACM0}
baud=${PCUTP_BAUD:-115200}
window=${PCUTP_WINDOW:-2}
block=${PCUTP_BLOCK:-4096}
log_file=${PCUTP_LOG:-/tmp/pcutpd.log}
python_bin=${PCUTP_PYTHON:-$repo_dir/.venv/bin/python}

is_running() {
  screen -S "$session" -Q select . >/dev/null 2>&1
}

status() {
  if is_running; then
    printf 'pcutpd is running: screen session %s\n' "$session"
    printf 'log: %s\n' "$log_file"
    return 0
  fi
  printf 'pcutpd is stopped: screen session %s\n' "$session"
  return 1
}

start() {
  if is_running; then
    printf 'pcutpd is already running; no second daemon was started.\n'
    status
    return 0
  fi
  if [[ ! -c "$port" ]]; then
    printf 'serial port is missing: %s\n' "$port" >&2
    return 1
  fi
  if [[ ! -x "$python_bin" ]]; then
    printf 'Python environment is missing: %s\n' "$python_bin" >&2
    return 1
  fi
  if ! command -v screen >/dev/null 2>&1; then
    printf 'screen is not installed.\n' >&2
    return 1
  fi

  local -a command=(
    env "PYTHONPATH=$repo_dir/src"
    "$python_bin" -u -m pcutp.daemon serve
    --port "$port" --baud "$baud" --window "$window" --block "$block"
    --trace --sound
  )
  command+=("$@")
  screen -L -Logfile "$log_file" -dmS "$session" "${command[@]}"
  sleep 0.2
  if ! is_running; then
    printf 'pcutpd exited during startup. Check %s\n' "$log_file" >&2
    return 1
  fi
  printf 'pcutpd started: %s @ %s, window=%s, block=%s\n' \
    "$port" "$baud" "$window" "$block"
  printf 'screen: screen -r %s\nlog: %s\n' "$session" "$log_file"
}

stop() {
  if ! is_running; then
    printf 'pcutpd is already stopped.\n'
    return 0
  fi
  screen -S "$session" -X quit
  printf 'pcutpd stopped.\n'
}

usage() {
  cat <<'EOF'
Usage: scripts/pcutpd.sh [start|restart|stop|status|log|rmb|help] [daemon options]

With no action, `start` is used. A running screen session is never duplicated.
Extra options after start/restart are passed to `pcutp.daemon serve`.

Environment overrides:
  PCUTP_PORT       serial device       (default: /dev/ttyACM0)
  PCUTP_BAUD       UART rate           (default: 115200)
  PCUTP_WINDOW     maximum window      (default: 2)
  PCUTP_BLOCK      block size          (default: 4096)
  PCUTP_SESSION    screen session      (default: pcutpd)
  PCUTP_LOG        screen log          (default: /tmp/pcutpd.log)
  PCUTP_PYTHON     Python executable   (default: repository .venv)

Examples:
  ./scripts/pcutpd.sh
  ./scripts/pcutpd.sh restart
  PCUTP_PORT=/dev/ttyUSB1 ./scripts/pcutpd.sh start --quiet
  ./scripts/pcutpd.sh log
  ./scripts/pcutpd.sh rmb
EOF
}

action=${1:-start}
if [[ $# -gt 0 ]]; then
  shift
fi
case "$action" in
  start) start "$@" ;;
  restart) stop; start "$@" ;;
  stop) stop ;;
  status) status ;;
  log) touch "$log_file"; tail -f "$log_file" ;;
  rmb) PYTHONPATH="$repo_dir/src" "$python_bin" "$repo_dir/scripts/pcutpctl.py" rmb ;;
  help|-h|--help) usage ;;
  *) printf 'unknown action: %s\n' "$action" >&2; usage >&2; exit 2 ;;
esac
