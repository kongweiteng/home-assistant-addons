#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${HUAXIN_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "Options file is missing"
    exit 1
fi

ACCOUNT_COUNT=$(jq -r '(.accounts // []) | length' "$OPTIONS_FILE")
if [ "$ACCOUNT_COUNT" -lt 1 ]; then
    bashio::log.fatal "Configure at least one water account"
    exit 1
fi

export HUAXIN_OPTIONS_FILE="$OPTIONS_FILE"
export HUAXIN_STATE_PATH="${HUAXIN_STATE_PATH:-/data/state.json}"
export HUAXIN_CACHE_KEY_PATH="${HUAXIN_CACHE_KEY_PATH:-/data/cache.key}"

bashio::log.info "Starting Huaxin Water for ${ACCOUNT_COUNT} configured account(s)"
exec python3 -m huaxin_water.main
