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
export HUAXIN_MQTT_TOPICS_PATH="${HUAXIN_MQTT_TOPICS_PATH:-/data/mqtt-topics.json}"

MQTT_SERVICE_JSON=""
if [ -n "${SUPERVISOR_TOKEN:-}" ]; then
    MQTT_SERVICE_JSON=$(
        printf '%s\n' \
            'silent' \
            'show-error' \
            'url = "http://supervisor/services/mqtt"' \
            "header = \"Authorization: Bearer ${SUPERVISOR_TOKEN}\"" \
            | curl --config - 2>/dev/null || true
    )
fi

export HUAXIN_MQTT_HOST
export HUAXIN_MQTT_PORT
export HUAXIN_MQTT_USERNAME
export HUAXIN_MQTT_PASSWORD
export HUAXIN_MQTT_SSL
if [ -n "$MQTT_SERVICE_JSON" ] && jq -e \
    '.result == "ok" and (.data.host | type == "string") and (.data.host | length > 0) and (.data.port | type == "number")' \
    >/dev/null 2>&1 <<<"$MQTT_SERVICE_JSON"; then
    HUAXIN_MQTT_HOST=$(jq -r '.data.host' <<<"$MQTT_SERVICE_JSON")
    HUAXIN_MQTT_PORT=$(jq -r '.data.port' <<<"$MQTT_SERVICE_JSON")
    HUAXIN_MQTT_USERNAME=$(jq -r '.data.username // ""' <<<"$MQTT_SERVICE_JSON")
    HUAXIN_MQTT_PASSWORD=$(jq -r '.data.password // ""' <<<"$MQTT_SERVICE_JSON")
    HUAXIN_MQTT_SSL=$(jq -r '.data.ssl // false' <<<"$MQTT_SERVICE_JSON")
    bashio::log.info "Using Supervisor-provided MQTT service"
else
    HUAXIN_MQTT_HOST=$(jq -r '.mqtt_host // ""' "$OPTIONS_FILE")
    HUAXIN_MQTT_PORT=$(jq -r '.mqtt_port // 1883' "$OPTIONS_FILE")
    HUAXIN_MQTT_USERNAME=$(jq -r '.mqtt_username // ""' "$OPTIONS_FILE")
    HUAXIN_MQTT_PASSWORD=$(jq -r '.mqtt_password // ""' "$OPTIONS_FILE")
    HUAXIN_MQTT_SSL=$(jq -r '.mqtt_ssl // false' "$OPTIONS_FILE")
    if [ -z "$HUAXIN_MQTT_HOST" ]; then
        bashio::log.fatal "Configure an MQTT broker or install a Supervisor MQTT service"
        exit 1
    fi
    bashio::log.info "Using configured MQTT broker"
fi
unset MQTT_SERVICE_JSON

bashio::log.info "Starting Huaxin Water with MQTT Discovery for ${ACCOUNT_COUNT} configured account(s)"
exec python3 -m huaxin_water.main
