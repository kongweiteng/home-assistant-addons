#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${RUNNER_RELAY_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "缺少 Add-on options 文件"
    exit 1
fi

RELAY_CONTROLLER_BASE_URL=$(jq -r '.controller_base_url // ""' "$OPTIONS_FILE")
RELAY_CONTROLLER_API_TOKEN=$(jq -r '.controller_api_token // ""' "$OPTIONS_FILE")
RELAY_API_TOKEN=$(jq -r '.relay_api_token // ""' "$OPTIONS_FILE")

case "$RELAY_CONTROLLER_BASE_URL" in
    http://[A-Za-z0-9_-]*:[0-9]*) ;;
    *)
        bashio::log.fatal "controller_base_url 必须是 Add-on 内部 HTTP 地址"
        exit 1
        ;;
esac
if [ "${#RELAY_CONTROLLER_API_TOKEN}" -lt 32 ] || [ "${#RELAY_API_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "请配置至少 32 个字符的 Controller 与 Relay 内部 Token"
    exit 1
fi

export RELAY_CONTROLLER_BASE_URL
export RELAY_CONTROLLER_API_TOKEN
export RELAY_API_TOKEN
export RELAY_MAX_CONNECTIONS=$(jq -r '.max_connections // 64' "$OPTIONS_FILE")
export RELAY_MAX_MESSAGE_BYTES=$(jq -r '.max_message_bytes // 32768' "$OPTIONS_FILE")
export RELAY_FIRST_FRAME_TIMEOUT_SECONDS=$(jq -r '.first_frame_timeout_seconds // 10' "$OPTIONS_FILE")
export RELAY_MESSAGES_PER_MINUTE=$(jq -r '.messages_per_minute // 120' "$OPTIONS_FILE")
export RELAY_CONTROLLER_TIMEOUT_SECONDS=$(jq -r '.controller_timeout_seconds // 10' "$OPTIONS_FILE")
export NO_PROXY="localhost,127.0.0.1,::1,supervisor,homeassistant,hassio,codex-controller,codex_runner_relay"

bashio::log.info "启动 Codex Runner Relay；仅启用 WSS 数据面与内部发布接口"
exec python3 -m codex_runner_relay.main
