#!/usr/bin/env bash
# Lifecycle helpers for the optional MQTT notification bridge.

notification_bridge_validate_options() {
    if [ "${NOTIFICATION_BRIDGE_ENABLED:-false}" != "true" ]; then
        return 0
    fi
    if [ -z "${NOTIFICATION_MQTT_HOST:-}" ]; then
        echo "[run] FATAL: notification_mqtt_host is required when the notification bridge is enabled" >&2
        return 1
    fi
    if ! [[ "${NOTIFICATION_MQTT_PORT:-}" =~ ^[0-9]+$ ]] \
        || [ "$NOTIFICATION_MQTT_PORT" -lt 1 ] \
        || [ "$NOTIFICATION_MQTT_PORT" -gt 65535 ]; then
        echo "[run] FATAL: notification_mqtt_port must be from 1 to 65535" >&2
        return 1
    fi
    if [ -z "${NOTIFICATION_MQTT_USERNAME//[[:space:]]/}" ] \
        || [ -z "${NOTIFICATION_MQTT_PASSWORD:-}" ]; then
        echo "[run] FATAL: notification MQTT username and password are required" >&2
        return 1
    fi
    if [ -z "${NOTIFICATION_ALLOWED_AUDIENCES:-}" ]; then
        echo "[run] FATAL: at least one notification audience is required" >&2
        return 1
    fi
}

notification_bridge_start() {
    if [ "${NOTIFICATION_BRIDGE_ENABLED:-false}" != "true" ]; then
        echo "[run] Notification bridge: disabled"
        NOTIFICATION_BRIDGE_PID=""
        return 0
    fi

    local data_dir="${PRIMARY_HOME}/notification-bridge"
    local log_dir="${PRIMARY_HOME}/logs"
    mkdir -p "$data_dir" "$log_dir"
    chmod 700 "$data_dir"

    echo "[run] Starting MQTT notification bridge..."
    env \
        NOTIFICATION_MQTT_HOST="$NOTIFICATION_MQTT_HOST" \
        NOTIFICATION_MQTT_PORT="$NOTIFICATION_MQTT_PORT" \
        NOTIFICATION_MQTT_USERNAME="$NOTIFICATION_MQTT_USERNAME" \
        NOTIFICATION_MQTT_PASSWORD="$NOTIFICATION_MQTT_PASSWORD" \
        NOTIFICATION_MQTT_TLS="$NOTIFICATION_MQTT_TLS" \
        NOTIFICATION_MQTT_CLIENT_ID="hermes-notification-bridge-v1" \
        NOTIFICATION_ALLOWED_AUDIENCES="$NOTIFICATION_ALLOWED_AUDIENCES" \
        NOTIFICATION_HERMES_BIN="$VENV_DIR/bin/hermes" \
        NOTIFICATION_HERMES_HOME="$PRIMARY_HOME" \
        NOTIFICATION_DATA_DIR="$data_dir" \
        NOTIFICATION_ADDON_VERSION="${ADDON_VERSION:-unknown}" \
        /usr/bin/python3 /usr/local/bin/hermes-notification-bridge \
        >> "$log_dir/notification-bridge.log" 2>&1 &
    NOTIFICATION_BRIDGE_PID=$!
    echo "[run] Notification bridge PID: $NOTIFICATION_BRIDGE_PID"
}

notification_bridge_stop() {
    local pid="${NOTIFICATION_BRIDGE_PID:-}"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        kill -TERM "$pid" 2>/dev/null || true
        local waited=0
        while kill -0 "$pid" 2>/dev/null && [ "$waited" -lt 10 ]; do
            sleep 1
            waited=$((waited + 1))
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        echo "[run] Notification bridge stopped"
    fi
    NOTIFICATION_BRIDGE_PID=""
}

notification_bridge_supervise() {
    if [ "${NOTIFICATION_BRIDGE_ENABLED:-false}" != "true" ]; then
        return 0
    fi
    local pid="${NOTIFICATION_BRIDGE_PID:-}"
    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        local exit_code=1
        if [ -n "$pid" ]; then
            set +e
            wait "$pid" 2>/dev/null
            exit_code=$?
            set -e
        fi
        echo "[run] Notification bridge exited (code: $exit_code), restarting in 5s..."
        sleep 5
        notification_bridge_start
    fi
}
