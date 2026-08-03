#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${WEIXIN_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "缺少 Add-on options 文件"
    exit 1
fi

WEIXIN_ATTACHMENT_API_TOKEN=$(jq -r '.attachment_api_token // ""' "$OPTIONS_FILE")
if [ "${#WEIXIN_ATTACHMENT_API_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "请配置至少 32 个字符的附件 API Token"
    exit 1
fi

export WEIXIN_ATTACHMENT_API_TOKEN
export WEIXIN_POLLER_ENABLED=$(jq -r '.poller_enabled // false' "$OPTIONS_FILE")
export WEIXIN_ACTIVATION_CONFIRMATION=$(jq -r '.activation_confirmation // ""' "$OPTIONS_FILE")
export WEIXIN_CONTROLLER_BASE_URL=$(jq -r '.controller_base_url // ""' "$OPTIONS_FILE")
export WEIXIN_CONTROLLER_API_TOKEN=$(jq -r '.controller_api_token // ""' "$OPTIONS_FILE")
export WEIXIN_ACCOUNT_ID=$(jq -r '.account_id // ""' "$OPTIONS_FILE")
export WEIXIN_ILINK_TOKEN=$(jq -r '.ilink_token // ""' "$OPTIONS_FILE")
export WEIXIN_ILINK_BASE_URL=$(jq -r '.ilink_base_url // "https://ilinkai.weixin.qq.com"' "$OPTIONS_FILE")
export WEIXIN_CDN_BASE_URL=$(jq -r '.cdn_base_url // "https://novac2c.cdn.weixin.qq.com/c2c"' "$OPTIONS_FILE")
export WEIXIN_SELF_USER_ID=$(jq -r '.self_user_id // ""' "$OPTIONS_FILE")
export WEIXIN_ALLOWED_USER_IDS_JSON=$(jq -c '.allowed_user_ids // []' "$OPTIONS_FILE")
export WEIXIN_MAX_MEDIA_BYTES=$(jq -r '.max_media_bytes // 20971520' "$OPTIONS_FILE")
export WEIXIN_SPOOL_TTL_SECONDS=$(jq -r '.spool_ttl_seconds // 86400' "$OPTIONS_FILE")
export WEIXIN_DATA_DIR=/data
export WEIXIN_DATABASE_PATH=/data/gateway.sqlite3

if [ "$WEIXIN_POLLER_ENABLED" = "true" ] && [ "$WEIXIN_ACTIVATION_CONFIRMATION" != "HERMES_POLLER_STOPPED" ]; then
    bashio::log.fatal "启动真实 poller 前必须确认 Hermes poller 已停止"
    exit 1
fi

bashio::log.info "启动 Weixin Gateway，poller_enabled=${WEIXIN_POLLER_ENABLED}"
exec python3 -m weixin_gateway.main
