#!/bin/sh
set -eu

log() {
  level="${1:-INFO}"
  shift || true
  printf '%s %s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$level" "all-in-one" "$*"
}

FS_ESL_HOST="${FS_ESL_HOST:-127.0.0.1}"
FS_ESL_PORT="${FS_ESL_PORT:-8021}"
FS_START_TIMEOUT="${FS_START_TIMEOUT:-120}"

APP_PID=""
FS_PID=""

stop_children() {
  log INFO "Stopping services"
  if [ -n "$APP_PID" ] && kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" 2>/dev/null || true
  fi
  if [ -n "$FS_PID" ] && kill -0 "$FS_PID" 2>/dev/null; then
    kill "$FS_PID" 2>/dev/null || true
  fi
  wait 2>/dev/null || true
}

trap 'stop_children; exit 0' INT TERM

log INFO "Preparing FreeSWITCH configuration"
/usr/local/bin/freeswitch-entrypoint.sh /bin/true

log INFO "Starting FreeSWITCH"
/usr/local/freeswitch/bin/freeswitch -nonat &
FS_PID="$!"

log INFO "Waiting for FreeSWITCH ESL at ${FS_ESL_HOST}:${FS_ESL_PORT}"
i=0
while ! nc -z "$FS_ESL_HOST" "$FS_ESL_PORT"; do
  i=$((i + 1))
  if [ "$i" -ge "$FS_START_TIMEOUT" ]; then
    log CRITICAL "FreeSWITCH ESL did not become ready within ${FS_START_TIMEOUT}s"
    stop_children
    exit 1
  fi
  if ! kill -0 "$FS_PID" 2>/dev/null; then
    log CRITICAL "FreeSWITCH exited before ESL became ready"
    stop_children
    exit 1
  fi
  sleep 1
done

FS_CLI="/usr/local/freeswitch/bin/fs_cli"

if [ "${FS_SIPTRACE:-0}" = "1" ]; then
  log INFO "Enabling FreeSWITCH SIP trace"
  "$FS_CLI" -H "$FS_ESL_HOST" -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "console loglevel ${FS_SIPTRACE_LOGLEVEL:-debug}" || true
  "$FS_CLI" -H "$FS_ESL_HOST" -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "sofia global siptrace on" || true
  "$FS_CLI" -H "$FS_ESL_HOST" -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "sofia profile internal siptrace on" || true
  "$FS_CLI" -H "$FS_ESL_HOST" -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "sofia profile external siptrace on" || true
else
  log INFO "Disabling FreeSWITCH SIP trace"
  "$FS_CLI" -H "$FS_ESL_HOST" -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "console loglevel ${FS_CONSOLE_LOGLEVEL:-info}" || true
  "$FS_CLI" -H "$FS_ESL_HOST" -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "sofia global siptrace off" || true
  "$FS_CLI" -H "$FS_ESL_HOST" -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "sofia profile internal siptrace off" || true
  "$FS_CLI" -H "$FS_ESL_HOST" -P "$FS_ESL_PORT" -p "$FS_ESL_PASSWORD" -x "sofia profile external siptrace off" || true
fi

log INFO "Starting PersonaPlex app"
python3.12 /app/app.py &
APP_PID="$!"

while true; do
  if ! kill -0 "$FS_PID" 2>/dev/null; then
    log CRITICAL "FreeSWITCH exited"
    stop_children
    exit 1
  fi
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    log CRITICAL "PersonaPlex app exited"
    stop_children
    exit 1
  fi
  sleep 2
done
