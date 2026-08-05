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
export WEIXIN_OWNER_PAIRING_ENABLED=$(jq -r '.owner_pairing_enabled // false' "$OPTIONS_FILE")
export WEIXIN_ACTIVATION_CONFIRMATION=$(jq -r '.activation_confirmation // ""' "$OPTIONS_FILE")
export WEIXIN_CONTROLLER_BASE_URL=$(jq -r '.controller_base_url // ""' "$OPTIONS_FILE")
export WEIXIN_CONTROLLER_API_TOKEN=$(jq -r '.controller_api_token // ""' "$OPTIONS_FILE")
export WEIXIN_CONTROLLER_INGRESS_BASE_URL=$(jq -r '.controller_ingress_base_url // ""' "$OPTIONS_FILE")
export WEIXIN_ACCOUNT_ID=$(jq -r '.account_id // ""' "$OPTIONS_FILE")
export WEIXIN_ILINK_TOKEN=$(jq -r '.ilink_token // ""' "$OPTIONS_FILE")
export WEIXIN_ILINK_BASE_URL=$(jq -r '.ilink_base_url // "https://ilinkai.weixin.qq.com"' "$OPTIONS_FILE")
export WEIXIN_CDN_BASE_URL=$(jq -r '.cdn_base_url // "https://novac2c.cdn.weixin.qq.com/c2c"' "$OPTIONS_FILE")
export WEIXIN_SELF_USER_ID=$(jq -r '.self_user_id // ""' "$OPTIONS_FILE")
export WEIXIN_ALLOWED_USER_IDS_JSON=$(jq -c '.allowed_user_ids // []' "$OPTIONS_FILE")
export WEIXIN_MAX_MEDIA_BYTES=$(jq -r '.max_media_bytes // 20971520' "$OPTIONS_FILE")
export WEIXIN_MAX_ACTIVE_IDENTITIES=$(jq -r '.max_active_identities // 5' "$OPTIONS_FILE")
export WEIXIN_SPOOL_TTL_SECONDS=$(jq -r '.spool_ttl_seconds // 86400' "$OPTIONS_FILE")
export WEIXIN_DATA_DIR=/data
export WEIXIN_DATABASE_PATH=/data/gateway.sqlite3
export WEIXIN_ADDON_VERSION=0.3.0
export WEIXIN_NOTIFICATION_BRIDGE_ENABLED=$(jq -r '.notification_bridge_enabled // false' "$OPTIONS_FILE")
export WEIXIN_NOTIFICATION_MQTT_HOST=$(jq -r '.notification_mqtt_host // ""' "$OPTIONS_FILE")
export WEIXIN_NOTIFICATION_MQTT_PORT=$(jq -r '.notification_mqtt_port // 1883' "$OPTIONS_FILE")
export WEIXIN_NOTIFICATION_MQTT_USERNAME=$(jq -r '.notification_mqtt_username // ""' "$OPTIONS_FILE")
export WEIXIN_NOTIFICATION_MQTT_PASSWORD=$(jq -r '.notification_mqtt_password // ""' "$OPTIONS_FILE")
export WEIXIN_NOTIFICATION_MQTT_TLS=$(jq -r '.notification_mqtt_tls // false' "$OPTIONS_FILE")
export WEIXIN_NOTIFICATION_ALLOWED_AUDIENCES=$(jq -r '(.notification_allowed_audiences // ["owner"]) | join(",")' "$OPTIONS_FILE")
export WEIXIN_REMOTE_WORK_ENABLED=$(jq -r '.remote_work_enabled // false' "$OPTIONS_FILE")
export WEIXIN_REMOTE_WORK_MQTT_HOST=$(jq -r '.remote_work_mqtt_host // ""' "$OPTIONS_FILE")
export WEIXIN_REMOTE_WORK_MQTT_PORT=$(jq -r '.remote_work_mqtt_port // 1883' "$OPTIONS_FILE")
export WEIXIN_REMOTE_WORK_MQTT_USERNAME=$(jq -r '.remote_work_mqtt_username // ""' "$OPTIONS_FILE")
export WEIXIN_REMOTE_WORK_MQTT_PASSWORD=$(jq -r '.remote_work_mqtt_password // ""' "$OPTIONS_FILE")
export WEIXIN_REMOTE_WORK_MQTT_TLS=$(jq -r '.remote_work_mqtt_tls // false' "$OPTIONS_FILE")
export WEIXIN_REMOTE_WORK_TTL_SECONDS=$(jq -r '.remote_work_ttl_seconds // 1800' "$OPTIONS_FILE")

if [ "$WEIXIN_POLLER_ENABLED" = "true" ] && [ "$WEIXIN_ACTIVATION_CONFIRMATION" != "HERMES_POLLER_STOPPED" ]; then
    bashio::log.fatal "启动真实 poller 前必须确认 Hermes poller 已停止"
    exit 1
fi

if [ "$WEIXIN_NOTIFICATION_BRIDGE_ENABLED" = "true" ]; then
    if [ -z "$WEIXIN_NOTIFICATION_MQTT_HOST" ] || [ -z "$WEIXIN_NOTIFICATION_MQTT_USERNAME" ] || [ -z "$WEIXIN_NOTIFICATION_MQTT_PASSWORD" ]; then
        bashio::log.fatal "启用主动通知时必须配置 MQTT host、username 和 password"
        exit 1
    fi
    if [ "$WEIXIN_NOTIFICATION_ALLOWED_AUDIENCES" != "owner" ]; then
        bashio::log.fatal "主动通知 v1 仅允许 owner audience"
        exit 1
    fi
fi

if [ "$WEIXIN_REMOTE_WORK_ENABLED" = "true" ]; then
    if [ -z "$WEIXIN_REMOTE_WORK_MQTT_HOST" ] || [ -z "$WEIXIN_REMOTE_WORK_MQTT_USERNAME" ] || [ -z "$WEIXIN_REMOTE_WORK_MQTT_PASSWORD" ]; then
        bashio::log.fatal "启用 Remote Work 时必须配置专用 MQTT host、username 和 password"
        exit 1
    fi
    if [ "$WEIXIN_NOTIFICATION_BRIDGE_ENABLED" = "true" ] && [ "$WEIXIN_REMOTE_WORK_MQTT_USERNAME" = "$WEIXIN_NOTIFICATION_MQTT_USERNAME" ]; then
        bashio::log.fatal "Remote Work 不得复用主动通知 MQTT 账户"
        exit 1
    fi
fi

bashio::log.info "启动 Weixin Gateway，poller_enabled=${WEIXIN_POLLER_ENABLED}，notification_bridge_enabled=${WEIXIN_NOTIFICATION_BRIDGE_ENABLED}，remote_work_enabled=${WEIXIN_REMOTE_WORK_ENABLED}"
exec python3 -m weixin_gateway.main
