#!/bin/bash

set -Eeuo pipefail

readonly STORAGE_DIR="${WIZNOTE_STORAGE_DIR:-/wiz/storage}"
readonly OPTIONS_FILE="${WIZNOTE_OPTIONS_FILE:-${STORAGE_DIR}/options.json}"
readonly UPSTREAM_ENTRYPOINT="${WIZNOTE_ENTRYPOINT:-/wiz/app/entrypoint.sh}"
readonly LOCALTIME_PATH="${WIZNOTE_LOCALTIME_PATH:-/etc/localtime}"

upstream_pid=""

log() {
    printf '[wiznote-addon] %s\n' "$*"
}

read_option() {
    local option_name="$1"

    node -e '
const fs = require("fs");
const [file, key] = process.argv.slice(1);
const options = JSON.parse(fs.readFileSync(file, "utf8"));
const value = options[key];
if (value !== undefined && value !== null) {
  process.stdout.write(String(value));
}
' "$OPTIONS_FILE" "$option_name"
}

stop_service() {
    trap - TERM INT
    log "Stopping WizNote services for a consistent shutdown"

    if [[ "${WIZNOTE_SKIP_SERVICE_SHUTDOWN:-0}" != "1" ]]; then
        command -v pm2 >/dev/null 2>&1 && pm2 kill >/dev/null 2>&1 || true
        command -v nginx >/dev/null 2>&1 && nginx -s quit >/dev/null 2>&1 || true
        command -v redis-cli >/dev/null 2>&1 && redis-cli shutdown >/dev/null 2>&1 || true
        command -v mysqladmin >/dev/null 2>&1 \
            && mysqladmin --protocol=socket -uroot -paI9DCyNpEKWe9pn5 shutdown >/dev/null 2>&1 \
            || true
    fi

    if [[ -n "$upstream_pid" ]] && kill -0 "$upstream_pid" >/dev/null 2>&1; then
        kill -TERM "$upstream_pid" >/dev/null 2>&1 || true
        for _ in {1..20}; do
            kill -0 "$upstream_pid" >/dev/null 2>&1 || break
            sleep 0.5
        done
        kill -KILL "$upstream_pid" >/dev/null 2>&1 || true
        wait "$upstream_pid" 2>/dev/null || true
    fi

    exit 0
}

mkdir -p "$STORAGE_DIR"

if [[ ! -r "$OPTIONS_FILE" ]]; then
    log "ERROR: Home Assistant options file is not readable: $OPTIONS_FILE"
    exit 1
fi

admin_password="$(read_option admin_password)"
timezone="$(read_option timezone)"
timezone="${timezone:-Asia/Shanghai}"

if [[ ! -f "/usr/share/zoneinfo/${timezone}" ]]; then
    log "ERROR: Unsupported timezone: ${timezone}"
    exit 1
fi

ln -snf "/usr/share/zoneinfo/${timezone}" "$LOCALTIME_PATH"
export TZ="$timezone"

if [[ -n "$admin_password" ]]; then
    export ADMIN_PASSWORD="$admin_password"
elif [[ ! -f "${STORAGE_DIR}/index/.runonce" ]]; then
    log "WARNING: No initial admin password is configured."
    log "WARNING: WizNote will use its upstream default; change it immediately after first login."
fi

if [[ ! -x "$UPSTREAM_ENTRYPOINT" && ! -r "$UPSTREAM_ENTRYPOINT" ]]; then
    log "ERROR: Upstream entrypoint is not available: $UPSTREAM_ENTRYPOINT"
    exit 1
fi

trap stop_service TERM INT

log "Starting WizNote with persistent storage at ${STORAGE_DIR}"
bash "$UPSTREAM_ENTRYPOINT" &
upstream_pid="$!"

exit_code=0
wait "$upstream_pid" || exit_code="$?"
log "WizNote upstream process exited with status ${exit_code}"
exit "$exit_code"
