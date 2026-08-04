#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${MANAGER_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "Options file is missing"
    exit 1
fi

MANAGER_API_TOKEN=$(jq -r '.manager_api_token // ""' "$OPTIONS_FILE")
MANAGER_RESTART_ADDON_ALLOWLIST=$(jq -r '(.restart_addon_allowlist // []) | map(select(length > 0)) | join(",")' "$OPTIONS_FILE")
MANAGER_MAX_REQUEST_BYTES=$(jq -r '.max_request_bytes // 32768' "$OPTIONS_FILE")
MANAGER_SUPERVISOR_TIMEOUT_SECONDS=$(jq -r '.supervisor_timeout_seconds // 5' "$OPTIONS_FILE")

if [ "${#MANAGER_API_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "Configure a manager API token with at least 32 characters"
    exit 1
fi
if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    bashio::log.fatal "Home Assistant Supervisor credential is unavailable"
    exit 1
fi

export MANAGER_API_TOKEN
export MANAGER_RESTART_ADDON_ALLOWLIST
export MANAGER_MAX_REQUEST_BYTES
export MANAGER_SUPERVISOR_TIMEOUT_SECONDS
export MANAGER_SUPERVISOR_BASE_URL="http://supervisor"
export MANAGER_SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN"
unset SUPERVISOR_TOKEN

bashio::log.info "Starting HA Manager Executor in read-only shadow mode"
exec python3 -m ha_manager_executor.main
