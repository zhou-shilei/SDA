#!/usr/bin/env bash
set -euo pipefail

studio_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_dir="$(dirname -- "$studio_dir")"
state_dir="$project_dir/.studio"
pid_file="$state_dir/studio.pid"
log_file="$state_dir/studio.log"
if [[ -n "${SDA_STUDIO_PYTHON:-}" ]]; then
  studio_python="$SDA_STUDIO_PYTHON"
elif [[ -x "$project_dir/../envs/sda/bin/python" ]]; then
  studio_python="$project_dir/../envs/sda/bin/python"
else
  studio_python="python"
fi
studio_port="${SDA_STUDIO_PORT:-4182}"

running() {
  [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null
}

case "${1:-start}" in
  start)
    if running; then
      echo "SDA Studio is running (PID $(cat "$pid_file"))"
      exit 0
    fi
    mkdir -p "$state_dir"
    nohup setsid "$studio_python" "$studio_dir/server.py" --port "$studio_port" > "$log_file" 2>&1 < /dev/null &
    studio_pid=$!
    echo "$studio_pid" > "$pid_file"
    for attempt in {1..30}; do
      if ! kill -0 "$studio_pid" 2>/dev/null; then
        cat "$log_file"
        rm -f "$pid_file"
        exit 1
      fi
      if "$studio_python" -c 'import sys,urllib.request; urllib.request.urlopen(sys.argv[1],timeout=1)' "http://127.0.0.1:$studio_port/api/health" 2>/dev/null; then
        echo "SDA Studio: http://127.0.0.1:$studio_port/studio/"
        exit 0
      fi
      sleep 0.2
    done
    echo "Startup is taking longer than expected; see $log_file"
    exit 1
    ;;
  stop)
    if running; then
      kill "$(cat "$pid_file")"
      for attempt in {1..50}; do
        if ! running; then break; fi
        sleep 0.1
      done
      if running; then
        echo "SDA Studio is still stopping; retry after its worker exits"
        exit 1
      fi
      echo "SDA Studio stopped"
    fi
    rm -f "$pid_file"
    ;;
  restart)
    if running; then
      "$studio_python" - "$state_dir/jobs.json" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
jobs = json.loads(path.read_text()) if path.exists() else {}
active = [identity for identity, job in jobs.items()
          if job.get("status") in {"queued", "processing"}]
if active:
    print("SDA Studio 暂不重启：解析任务仍在运行（" + ", ".join(active) + "）。请等待完成后重启。", file=sys.stderr)
    sys.exit(1)
PY
    fi
    bash "$0" stop
    bash "$0" start
    ;;
  status)
    if running; then echo "SDA Studio is running (PID $(cat "$pid_file"))";
    else echo "SDA Studio is stopped"; fi
    ;;
  logs)
    tail -n 80 "$log_file"
    ;;
  *) echo "Usage: $0 {start|stop|restart|status|logs}"; exit 2 ;;
esac
