#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${CONTROLLER_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "缺少 Add-on options 文件"
    exit 1
fi

CONTROLLER_API_TOKEN=$(jq -r '.internal_api_token // ""' "$OPTIONS_FILE")
if [ "${#CONTROLLER_API_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "请配置至少 32 个字符的内部 API Token"
    exit 1
fi

export CONTROLLER_API_TOKEN
export CONTROLLER_INTAKE_ENABLED=$(jq -r '.intake_enabled // false' "$OPTIONS_FILE")
export CONTROLLER_AUTH_MODE=$(jq -r '.auth_mode // "chatgpt_device_code"' "$OPTIONS_FILE")
export CONTROLLER_OPENAI_BASE_URL=$(jq -r '.openai_base_url // ""' "$OPTIONS_FILE")
export CONTROLLER_CODEX_MODEL=$(jq -r '.codex_model // ""' "$OPTIONS_FILE")
case "$CONTROLLER_AUTH_MODE" in
    chatgpt_device_code)
        unset CONTROLLER_OPENAI_API_KEY_FD
        ;;
    api_key)
        exec 3< <(jq -j '.openai_api_key // ""' "$OPTIONS_FILE")
        export CONTROLLER_OPENAI_API_KEY_FD=3
        ;;
    *)
        bashio::log.fatal "auth_mode 只允许 chatgpt_device_code 或 api_key"
        exit 1
        ;;
esac
export CONTROLLER_LEDGER_BASE_URL=$(jq -r '.ledger_base_url // ""' "$OPTIONS_FILE")
export CONTROLLER_LEDGER_API_TOKEN=$(jq -r '.ledger_api_token // ""' "$OPTIONS_FILE")
export CONTROLLER_GATEWAY_BASE_URL=$(jq -r '.gateway_base_url // ""' "$OPTIONS_FILE")
export CONTROLLER_GATEWAY_ATTACHMENT_TOKEN=$(jq -r '.gateway_attachment_token // ""' "$OPTIONS_FILE")
export CONTROLLER_OPERATIONS_BASE_URL=$(jq -r '.operations_base_url // ""' "$OPTIONS_FILE")
export CONTROLLER_OPERATIONS_API_TOKEN=$(jq -r '.operations_api_token // ""' "$OPTIONS_FILE")
export CONTROLLER_MAX_REQUEST_BYTES=$(jq -r '.max_request_bytes // 1048576' "$OPTIONS_FILE")
export CONTROLLER_MAX_QUEUE=$(jq -r '.max_queue // 200' "$OPTIONS_FILE")
export CONTROLLER_MAX_RESULT_CHARS=$(jq -r '.max_result_chars // 12000' "$OPTIONS_FILE")
export CONTROLLER_MAX_MEDIA_BYTES=$(jq -r '.max_media_bytes // 1073741824' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_CENTER_V2_ENABLED=$(jq -r '.runner_center_v2_enabled // true' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_ONLINE_SECONDS=$(jq -r '.runner_online_seconds // 30' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_OFFLINE_SECONDS=$(jq -r '.runner_offline_seconds // 90' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_LEASE_TTL_SECONDS=$(jq -r '.runner_lease_ttl_seconds // 60' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_TASK_TTL_SECONDS=$(jq -r '.runner_task_ttl_seconds // 1800' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_RELAY_BASE_URL=$(jq -r '.runner_relay_base_url // ""' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_RELAY_API_TOKEN=$(jq -r '.runner_relay_api_token // ""' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_RELAY_CONTROLLER_API_TOKEN=$(jq -r '.runner_relay_controller_api_token // ""' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_RELAY_PUBLIC_URL=$(jq -r '.runner_relay_public_url // ""' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_INSTALLER_MANIFEST_URL=$(jq -r '.runner_installer_manifest_url // ""' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_INSTALLER_MANIFEST_SHA256=$(jq -r '.runner_installer_manifest_sha256 // ""' "$OPTIONS_FILE")
export CONTROLLER_RUNNER_RELAY_TIMEOUT_SECONDS=$(jq -r '.runner_relay_timeout_seconds // 10' "$OPTIONS_FILE")
export NO_PROXY="localhost,127.0.0.1,::1,supervisor,homeassistant,hassio,renovation-hub,weixin-gateway,ha-operations-broker,local-renovation-hub,local-weixin-gateway,local-ha-operations-broker,local-codex-controller,local-codex-runner-relay"
export CONTROLLER_DATA_DIR=/data
export CONTROLLER_DATABASE_PATH=/data/controller.sqlite3
export CONTROLLER_CODEX_HOME=/data/codex-home
export CONTROLLER_WORKSPACE=/data/workspace
export CONTROLLER_MCP_SOCKET=/data/runtime/tool-proxy.sock

bashio::log.info "启动 Codex Controller；auth_mode=${CONTROLLER_AUTH_MODE}，intake_enabled=${CONTROLLER_INTAKE_ENABLED}，runner_center_v2_enabled=${CONTROLLER_RUNNER_CENTER_V2_ENABLED}，relay_configured=$([ -n "$CONTROLLER_RUNNER_RELAY_BASE_URL" ] && printf true || printf false)"
exec python3 -m codex_controller.main
