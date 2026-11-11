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
studio_host="${SDA_STUDIO_HOST:-127.0.0.1}"
health_host="$studio_host"
if [[ "$health_host" == "0.0.0.0" ]]; then
  health_host="127.0.0.1"
fi

running() {
  [[ -f "$pid_file" ]] && kill -0 "$(cat "$pid_file")" 2>/dev/null
}

supervisorctl_bin="/nix/var/ml-platform-env/bin/supervisorctl"
supervisor_config="/mlplatform/supervisord/supervisord.conf"
if [[ -x "$supervisorctl_bin" && -f "$supervisor_config" ]]; then
  supervisor_status="$("$supervisorctl_bin" -c "$supervisor_config" status seqdata-studio 2>/dev/null || true)"
  if [[ "$supervisor_status" =~ ^seqdata-studio[[:space:]]+(RUNNING|STARTING|STOPPED|BACKOFF|STOPPING|EXITED|FATAL|UNKNOWN)([[:space:]]|$) ]]; then
    case "${1:-start}" in
      start)
        if [[ "$supervisor_status" == *RUNNING* ]]; then
          echo "$supervisor_status"
        else
          "$supervisorctl_bin" -c "$supervisor_config" start seqdata-studio
        fi
        ;;
      stop)
        if [[ "$supervisor_status" == *RUNNING* ]]; then
          "$supervisorctl_bin" -c "$supervisor_config" stop seqdata-studio
        fi
        ;;
      restart)
        if [[ "$supervisor_status" == *RUNNING* ]]; then
          "$supervisorctl_bin" -c "$supervisor_config" restart seqdata-studio
        else
          "$supervisorctl_bin" -c "$supervisor_config" start seqdata-studio
        fi
        ;;
      status) echo "$supervisor_status" ;;
      logs) tail -n 80 "$state_dir/seqdata-supervisor.log" ;;
      *) echo "Usage: $0 {start|stop|restart|status|logs}"; exit 2 ;;
    esac
    exit 0
  fi
fi

case "${1:-start}" in
  start)
    if running; then
      echo "SDA Studio is running (PID $(cat "$pid_file"))"
      exit 0
    fi
    mkdir -p "$state_dir"
    nohup setsid "$studio_python" "$studio_dir/server.py" --host "$studio_host" --port "$studio_port" > "$log_file" 2>&1 < /dev/null &
    studio_pid=$!
    echo "$studio_pid" > "$pid_file"
    for attempt in {1..30}; do
      if ! kill -0 "$studio_pid" 2>/dev/null; then
        cat "$log_file"
        rm -f "$pid_file"
        exit 1
      fi
      if "$studio_python" -c 'import sys,urllib.request; urllib.request.urlopen(sys.argv[1],timeout=1)' "http://$health_host:$studio_port/api/health" 2>/dev/null; then
        echo "SDA Studio: http://$health_host:$studio_port/studio/"
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
      "$studio_python" - "$state_dir/jobs.json" "$studio_port" "$health_host" <<'PY'
import json
from pathlib import Path
import sys
from urllib.request import urlopen

# Queue-enabled HTTP servers do not own or stop the detached parser.
try:
    with urlopen(f"http://{sys.argv[3]}:{sys.argv[2]}/api/health", timeout=2) as response:
        if json.load(response).get("persistent_queue") is True:
            sys.exit(0)
except Exception:
    pass

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
