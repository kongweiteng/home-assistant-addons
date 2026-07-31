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
export JOURNEY_MQTT_HOST
export JOURNEY_MQTT_PORT
export JOURNEY_MQTT_USERNAME
export JOURNEY_MQTT_PASSWORD
export JOURNEY_MQTT_SSL

JOURNEY_MQTT_HOST=$(bashio::services mqtt "host")
JOURNEY_MQTT_PORT=$(bashio::services mqtt "port")
JOURNEY_MQTT_USERNAME=$(bashio::services mqtt "username")
JOURNEY_MQTT_PASSWORD=$(bashio::services mqtt "password")
JOURNEY_MQTT_SSL=$(bashio::services mqtt "ssl")

bashio::log.info "Starting Journey Analyzer for ${ENTITY_COUNT} selected entity/entities"
exec python3 -m journey_analyzer.main
