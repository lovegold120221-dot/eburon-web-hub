#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EBURON_HOME="${EBURON_HOME:-${HOME}/.hermes}"
PID_FILE="${EBURON_HUB_PID_FILE:-${EBURON_HOME}/webui.pid}"
LOG_FILE="${EBURON_HUB_LOG_FILE:-${EBURON_HOME}/webui.log}"
STATE_FILE="${EBURON_HUB_CTL_STATE_FILE:-${EBURON_HOME}/webui.ctl.env}"
DEFAULT_STATE_DIR="${EBURON_HUB_STATE_DIR:-${EBURON_HOME}/webui}"

usage() {
  cat <<'EOF'
Usage: ./ctl.sh <command> [args]

Commands:
  start [bootstrap args...]   Start Eburon Hub as a background daemon
  stop                        Stop the daemon started by ctl.sh
  restart [bootstrap args...] Stop, then start again
  status                      Show daemon, host/port, log, and health status
  logs [--lines N] [--follow|--no-follow]
                              Show the daemon log (defaults to tail -n 100 -f)
EOF
}

ensure_home() {
  mkdir -p "${EBURON_HOME}" "${DEFAULT_STATE_DIR}"
}

_load_repo_dotenv_preserving_env() {
  local env_file="${REPO_ROOT}/.env"
  [[ -f "${env_file}" ]] || return 0

  local -a preserved=()
  local line key value
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line#${line%%[![:space:]]*}}"
    [[ -z "${line}" || "${line}" == \#* || "${line}" != *=* ]] && continue
    key="${line%%=*}"
    key="${key#export }"
    key="${key//[[:space:]]/}"
    [[ "${key}" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ ${!key+x} ]]; then
      value="${!key}"
      preserved+=("${key}=${value}")
    fi
  done < "${env_file}"

  set -a
  # shellcheck source=/dev/null
  source "${env_file}"
  set +a

  local assignment
  if (( ${#preserved[@]} > 0 )); then
    for assignment in "${preserved[@]}"; do
      export "${assignment}"
    done
  fi
}

_find_python() {
  if [[ -n "${EBURON_HUB_PYTHON:-}" ]]; then
    printf '%s\n' "${EBURON_HUB_PYTHON}"
  elif command -v python3 >/dev/null 2>&1; then
    command -v python3
  elif command -v python >/dev/null 2>&1; then
    command -v python
  else
    echo "[ctl] Python 3 is required to run bootstrap.py" >&2
    return 1
  fi
}

_parse_launch_binding() {
  CTL_HOST="${EBURON_HUB_HOST:-127.0.0.1}"
  CTL_PORT="${EBURON_HUB_PORT:-8787}"
  local arg next_is_host=0 saw_port=0
  for arg in "$@"; do
    if (( next_is_host )); then
      CTL_HOST="${arg}"
      next_is_host=0
      continue
    fi
    case "${arg}" in
      --host)
        next_is_host=1
        ;;
      --host=*)
        CTL_HOST="${arg#--host=}"
        ;;
      --*)
        ;;
      *)
        if (( ! saw_port )) && [[ "${arg}" =~ ^[0-9]+$ ]]; then
          CTL_PORT="${arg}"
          saw_port=1
        fi
        ;;
    esac
  done
}

_build_bootstrap_args() {
  CTL_BOOTSTRAP_ARGS=()
  local arg next_is_host=0 saw_port=0
  for arg in "$@"; do
    if (( next_is_host )); then
      next_is_host=0
      continue
    fi
    case "${arg}" in
      --host)
        next_is_host=1
        ;;
      --host=*)
        ;;
      --*)
        CTL_BOOTSTRAP_ARGS+=("${arg}")
        ;;
      *)
        if (( ! saw_port )) && [[ "${arg}" =~ ^[0-9]+$ ]]; then
          saw_port=1
        else
          CTL_BOOTSTRAP_ARGS+=("${arg}")
        fi
        ;;
    esac
  done
}

_write_state() {
  local pid="$1" host="$2" port="$3"
  local state_dir="${EBURON_HUB_STATE_DIR:-${DEFAULT_STATE_DIR}}"
  {
    printf 'PID=%q\n' "${pid}"
    printf 'REPO_ROOT=%q\n' "${REPO_ROOT}"
    printf 'HOST=%q\n' "${host}"
    printf 'PORT=%q\n' "${port}"
    printf 'LOG_FILE=%q\n' "${LOG_FILE}"
    printf 'STATE_DIR=%q\n' "${state_dir}"
    printf 'STARTED_AT=%q\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "${STATE_FILE}"
}

_load_state_if_present() {
  if [[ -f "${STATE_FILE}" ]]; then
    # shellcheck source=/dev/null
    source "${STATE_FILE}"
  fi
}

_pid_from_file() {
  [[ -f "${PID_FILE}" ]] || return 1
  local pid
  pid="$(tr -d '[:space:]' < "${PID_FILE}")"
  [[ "${pid}" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "${pid}"
}

_is_alive() {
  local pid="$1"
  kill -0 "${pid}" >/dev/null 2>&1
}

_proc_args() {
  local pid="$1"
  ps -p "${pid}" -o args= 2>/dev/null || true
}

_pid_for_port() {
  local port="$1"
  command -v lsof >/dev/null 2>&1 || return 1
  lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null | head -n 1
}

_is_owned_webui_pid() {
  local pid="$1" args state_repo="${REPO_ROOT}"
  [[ -f "${STATE_FILE}" ]] && _load_state_if_present
  state_repo="${REPO_ROOT:-${state_repo}}"
  [[ "${state_repo}" == "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" ]] || return 1
  args="$(_proc_args "${pid}")"
  [[ -n "${args}" ]] || return 1
  [[ "${args}" == *"${state_repo}/bootstrap.py"* || "${args}" == *"${state_repo}/server.py"* || "${args}" == *"${state_repo}/start.sh"* ]]
}

_current_pid() {
  local pid port port_pid had_pid_file=0
  _load_state_if_present
  port="${PORT:-${EBURON_HUB_PORT:-8787}}"
  port_pid="$(_pid_for_port "${port}" || true)"
  [[ -f "${PID_FILE}" ]] && had_pid_file=1
  if pid="$(_pid_from_file 2>/dev/null)"; then
    if _is_alive "${pid}" && { _is_owned_webui_pid "${pid}" || [[ "${pid}" == "${port_pid}" ]]; }; then
      printf '%s\n' "${pid}"
      return 0
    fi
  fi
  if (( had_pid_file )); then
    return 1
  fi
  [[ "${port_pid}" =~ ^[0-9]+$ ]] || return 1
  if _is_alive "${port_pid}"; then
    printf '%s\n' "${port_pid}"
    return 0
  fi
  return 1
}

_clear_stale_pid() {
  if [[ -f "${PID_FILE}" ]]; then
    rm -f "${PID_FILE}" "${STATE_FILE}"
    echo "[ctl] Removed stale PID file: ${PID_FILE}"
  fi
}

start_cmd() {
  ensure_home
  _load_repo_dotenv_preserving_env
  export EBURON_HUB_STATE_DIR="${EBURON_HUB_STATE_DIR:-${DEFAULT_STATE_DIR}}"
  mkdir -p "${EBURON_HUB_STATE_DIR}"
  _parse_launch_binding "$@"
  _build_bootstrap_args "$@"
  export EBURON_HUB_HOST="${CTL_HOST}"
  export EBURON_HUB_PORT="${CTL_PORT}"

  local existing_pid
  if existing_pid="$(_current_pid 2>/dev/null)"; then
    printf '%s\n' "${existing_pid}" > "${PID_FILE}"
    _write_state "${existing_pid}" "${CTL_HOST}" "${CTL_PORT}"
    echo "[ctl] Eburon Hub is already running (PID ${existing_pid})"
    return 0
  fi
  _clear_stale_pid >/dev/null 2>&1 || true

  local python_exe pid
  local -a launch_args
  python_exe="$(_find_python)"
  : >> "${LOG_FILE}"

  if [[ -n "${EBURON_HUB_PYTHON:-}" ]]; then
    launch_args=(--no-browser --foreground --host "${CTL_HOST}" "${CTL_PORT}")
    if (( ${#CTL_BOOTSTRAP_ARGS[@]} > 0 )); then
      launch_args+=("${CTL_BOOTSTRAP_ARGS[@]}")
    fi
    (
      cd "${REPO_ROOT}"
      exec "${python_exe}" "${REPO_ROOT}/bootstrap.py" "${launch_args[@]}"
    ) >> "${LOG_FILE}" 2>&1 &
    pid=$!
    sleep 0.35
  else
    launch_args=(--no-browser --host "${CTL_HOST}" "${CTL_PORT}")
    if (( ${#CTL_BOOTSTRAP_ARGS[@]} > 0 )); then
      launch_args+=("${CTL_BOOTSTRAP_ARGS[@]}")
    fi
    if ! (
      cd "${REPO_ROOT}"
      "${python_exe}" "${REPO_ROOT}/bootstrap.py" "${launch_args[@]}"
    ) >> "${LOG_FILE}" 2>&1; then
      echo "[ctl] Eburon Hub failed to start. Log: ${LOG_FILE}" >&2
      rm -f "${PID_FILE}" "${STATE_FILE}"
      return 1
    fi

    pid="$(_pid_for_port "${CTL_PORT}" || true)"
    if [[ -z "${pid}" || ! "${pid}" =~ ^[0-9]+$ ]]; then
      echo "[ctl] Eburon Hub reported ready, but no listener PID was found on ${CTL_HOST}:${CTL_PORT}. Log: ${LOG_FILE}" >&2
      rm -f "${PID_FILE}" "${STATE_FILE}"
      return 1
    fi
  fi

  sleep 0.15
  if ! _is_alive "${pid}"; then
    echo "[ctl] Eburon Hub failed to stay running. Log: ${LOG_FILE}" >&2
    rm -f "${PID_FILE}" "${STATE_FILE}"
    return 1
  fi
  printf '%s\n' "${pid}" > "${PID_FILE}"
  _write_state "${pid}" "${CTL_HOST}" "${CTL_PORT}"
  echo "[ctl] Started Eburon Hub (PID ${pid})"
  echo "[ctl] Bound: ${CTL_HOST}:${CTL_PORT}"
  echo "[ctl] Log: ${LOG_FILE}"
}

stop_cmd() {
  ensure_home
  local pid
  if ! pid="$(_current_pid 2>/dev/null)"; then
    if [[ -f "${PID_FILE}" ]]; then
      _clear_stale_pid
    else
      echo "[ctl] Eburon Hub is stopped"
      rm -f "${STATE_FILE}"
    fi
    return 0
  fi

  if ! _is_alive "${pid}" || ! _is_owned_webui_pid "${pid}"; then
    _clear_stale_pid
    return 0
  fi

  echo "[ctl] Stopping Eburon Hub (PID ${pid})"
  kill "${pid}" >/dev/null 2>&1 || true
  local i
  for i in {1..50}; do
    if ! _is_alive "${pid}"; then
      rm -f "${PID_FILE}" "${STATE_FILE}"
      echo "[ctl] Stopped"
      return 0
    fi
    sleep 0.1
  done

  echo "[ctl] Process did not exit after SIGTERM; sending SIGKILL" >&2
  kill -KILL "${pid}" >/dev/null 2>&1 || true
  rm -f "${PID_FILE}" "${STATE_FILE}"
}

_health_line() {
  local host="$1" port="$2" url result
  url="http://${host}:${port}/health"
  if command -v curl >/dev/null 2>&1; then
    if result="$(curl -fsS --max-time 2 "${url}" 2>/dev/null)"; then
      if command -v python3 >/dev/null 2>&1; then
        printf '%s' "${result}" | python3 -c 'import json,sys
try:
    data=json.load(sys.stdin)
    sessions=data.get("sessions", data.get("session_count", "?"))
    active=data.get("active_streams", "?")
    status=data.get("status", "ok")
    print(f"ok ({sessions} sessions, {active} active streams)" if status == "ok" else status)
except Exception:
    print("ok")'
      else
        echo "ok"
      fi
    else
      echo "unreachable (${url})"
    fi
  else
    echo "unknown (curl not found; ${url})"
  fi
}

status_cmd() {
  ensure_home
  _load_state_if_present
  local host="${HOST:-${EBURON_HUB_HOST:-127.0.0.1}}"
  local port="${PORT:-${EBURON_HUB_PORT:-8787}}"
  local log_path="${LOG_FILE}"
  local pid uptime health

  if pid="$(_current_pid 2>/dev/null)"; then
    uptime="$(ps -p "${pid}" -o etime= 2>/dev/null | sed 's/^ *//' || true)"
    health="$(_health_line "${host}" "${port}")"
    echo "● eburon-hub — running"
    echo "  PID:     ${pid}"
    echo "  Uptime:  ${uptime:-unknown}"
    echo "  Bound:   ${host}:${port}"
    echo "  Log:     ${log_path}"
    echo "  Health:  ${health}"
  else
    [[ -f "${PID_FILE}" ]] && _clear_stale_pid >/dev/null 2>&1 || true
    echo "● eburon-hub — stopped"
    echo "  PID:     -"
    echo "  Bound:   ${host}:${port}"
    echo "  Log:     ${log_path}"
    echo "  Health:  not checked"
  fi
}

logs_cmd() {
  ensure_home
  local lines=100 follow=1
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lines)
        shift
        lines="${1:-}"
        [[ "${lines}" =~ ^[0-9]+$ ]] || { echo "[ctl] --lines requires a number" >&2; return 2; }
        ;;
      --lines=*)
        lines="${1#--lines=}"
        [[ "${lines}" =~ ^[0-9]+$ ]] || { echo "[ctl] --lines requires a number" >&2; return 2; }
        ;;
      --follow|-f)
        follow=1
        ;;
      --no-follow)
        follow=0
        ;;
      *)
        echo "[ctl] Unknown logs option: $1" >&2
        return 2
        ;;
    esac
    shift
  done
  touch "${LOG_FILE}"
  if (( follow )); then
    tail -n "${lines}" -f "${LOG_FILE}"
  else
    tail -n "${lines}" "${LOG_FILE}"
  fi
}

cmd="${1:-}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "${cmd}" in
  start) start_cmd "$@" ;;
  stop) stop_cmd ;;
  restart) stop_cmd; start_cmd "$@" ;;
  status) status_cmd ;;
  logs) logs_cmd "$@" ;;
  -h|--help|help|"") usage ;;
  *) echo "[ctl] Unknown command: ${cmd}" >&2; usage >&2; exit 2 ;;
esac
