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
export CONTROLLER_DATA_DIR=/data
export CONTROLLER_DATABASE_PATH=/data/controller.sqlite3
export CONTROLLER_CODEX_HOME=/data/codex-home
export CONTROLLER_WORKSPACE=/data/workspace
export CONTROLLER_MCP_SOCKET=/data/runtime/tool-proxy.sock

bashio::log.info "启动 Codex Controller；正式认证仅允许 ChatGPT Device Code，intake_enabled=${CONTROLLER_INTAKE_ENABLED}"
exec python3 -m codex_controller.main
