#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${ESLINK_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "Options file is missing"
    exit 1
fi

ACCOUNT_COUNT=$(jq -r '(.accounts // []) | length' "$OPTIONS_FILE")
if [ "$ACCOUNT_COUNT" -lt 1 ]; then
    bashio::log.fatal "Configure at least one gas account"
    exit 1
fi

if [ "$(jq -r '.allow_insecure_http // false' "$OPTIONS_FILE")" != "true" ]; then
    bashio::log.fatal "ESLink currently uses plain HTTP; explicitly enable allow_insecure_http after reviewing the risk"
    exit 1
fi

if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    bashio::log.fatal "Home Assistant Supervisor credential is unavailable"
    exit 1
fi

install -d -m 0700 /data/chromium-profile

export ESLINK_OPTIONS_FILE="$OPTIONS_FILE"
export ESLINK_STATE_PATH="${ESLINK_STATE_PATH:-/data/state.json}"
export ESLINK_BROWSER_PROFILE="${ESLINK_BROWSER_PROFILE:-/data/chromium-profile}"
export ESLINK_BROWSER_BINARY="${ESLINK_BROWSER_BINARY:-/usr/bin/chromium}"
export ESLINK_HA_BASE_URL="http://supervisor/core/api"
export ESLINK_HA_TOKEN="$SUPERVISOR_TOKEN"
export ESLINK_PUBLISHER="ha_mqtt"

MQTT_SERVICE_JSON=$(
    printf '%s\n' \
        'silent' \
        'show-error' \
        'url = "http://supervisor/services/mqtt"' \
        "header = \"Authorization: Bearer ${SUPERVISOR_TOKEN}\"" \
        | curl --config - 2>/dev/null || true
)

if jq -e '.result == "ok" and (.data.host | type == "string")' \
    >/dev/null 2>&1 <<<"$MQTT_SERVICE_JSON"; then
    export ESLINK_PUBLISHER="direct_mqtt"
    export ESLINK_MQTT_HOST
    export ESLINK_MQTT_PORT
    export ESLINK_MQTT_USERNAME
    export ESLINK_MQTT_PASSWORD
    export ESLINK_MQTT_SSL
    ESLINK_MQTT_HOST=$(jq -r '.data.host' <<<"$MQTT_SERVICE_JSON")
    ESLINK_MQTT_PORT=$(jq -r '.data.port' <<<"$MQTT_SERVICE_JSON")
    ESLINK_MQTT_USERNAME=$(jq -r '.data.username // ""' <<<"$MQTT_SERVICE_JSON")
    ESLINK_MQTT_PASSWORD=$(jq -r '.data.password // ""' <<<"$MQTT_SERVICE_JSON")
    ESLINK_MQTT_SSL=$(jq -r '.data.ssl // false' <<<"$MQTT_SERVICE_JSON")
    bashio::log.info "Using Supervisor-provided MQTT service"
else
    bashio::log.warning "Supervisor MQTT service is unavailable; using Home Assistant mqtt.publish"
fi
unset MQTT_SERVICE_JSON

bashio::log.info "Starting ESLink Gas for ${ACCOUNT_COUNT} configured account(s)"
exec python3 -m eslink_gas.main
