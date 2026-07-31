#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${JOURNEY_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "Options file is missing"
    exit 1
fi

ENTITY_COUNT=$(jq -r '(.entity_ids // []) | length' "$OPTIONS_FILE")
if [ "$ENTITY_COUNT" -lt 1 ]; then
    bashio::log.fatal "Configure at least one person or device_tracker entity"
    exit 1
fi

if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    bashio::log.fatal "Home Assistant Supervisor credential is unavailable"
    exit 1
fi

export JOURNEY_OPTIONS_FILE="$OPTIONS_FILE"
export JOURNEY_DATABASE_PATH="${JOURNEY_DATABASE_PATH:-/data/journeys.db}"
export JOURNEY_HA_BASE_URL="http://supervisor/core/api"
export JOURNEY_HA_TOKEN="$SUPERVISOR_TOKEN"
export JOURNEY_PUBLISHER="ha_mqtt"

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
    export JOURNEY_PUBLISHER="direct_mqtt"
    export JOURNEY_MQTT_HOST
    export JOURNEY_MQTT_PORT
    export JOURNEY_MQTT_USERNAME
    export JOURNEY_MQTT_PASSWORD
    export JOURNEY_MQTT_SSL
    JOURNEY_MQTT_HOST=$(jq -r '.data.host' <<<"$MQTT_SERVICE_JSON")
    JOURNEY_MQTT_PORT=$(jq -r '.data.port' <<<"$MQTT_SERVICE_JSON")
    JOURNEY_MQTT_USERNAME=$(jq -r '.data.username // ""' <<<"$MQTT_SERVICE_JSON")
    JOURNEY_MQTT_PASSWORD=$(jq -r '.data.password // ""' <<<"$MQTT_SERVICE_JSON")
    JOURNEY_MQTT_SSL=$(jq -r '.data.ssl // false' <<<"$MQTT_SERVICE_JSON")
    bashio::log.info "Using Supervisor-provided MQTT service"
else
    bashio::log.warning "Supervisor MQTT service is unavailable; using Home Assistant mqtt.publish"
fi
unset MQTT_SERVICE_JSON

bashio::log.info "Starting Journey Analyzer for ${ENTITY_COUNT} selected entity/entities"
exec python3 -m journey_analyzer.main
