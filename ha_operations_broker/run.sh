#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${BROKER_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "Options file is missing"
    exit 1
fi

BROKER_API_TOKEN=$(jq -r '.broker_api_token // ""' "$OPTIONS_FILE")
BROKER_OWNER_HASHES=$(jq -r '(.trusted_owner_identity_hashes // []) | map(select(length > 0)) | join(",")' "$OPTIONS_FILE")
BROKER_MAX_REQUEST_BYTES=$(jq -r '.max_request_bytes // 32768' "$OPTIONS_FILE")
BROKER_SUPERVISOR_TIMEOUT_SECONDS=$(jq -r '.supervisor_timeout_seconds // 5' "$OPTIONS_FILE")

if [ "${#BROKER_API_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "Configure a broker API token with at least 32 characters"
    exit 1
fi
if [ -z "$BROKER_OWNER_HASHES" ]; then
    bashio::log.fatal "Configure at least one trusted owner identity hash"
    exit 1
fi
if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    bashio::log.fatal "Home Assistant Supervisor credential is unavailable"
    exit 1
fi

export BROKER_API_TOKEN
export BROKER_OWNER_HASHES
export BROKER_MAX_REQUEST_BYTES
export BROKER_SUPERVISOR_TIMEOUT_SECONDS
export BROKER_SUPERVISOR_BASE_URL="http://supervisor"
export BROKER_SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN"
unset SUPERVISOR_TOKEN

bashio::log.info "Starting read-only HA Operations Broker canary on the internal app network"
exec python3 -m operations_broker.main
