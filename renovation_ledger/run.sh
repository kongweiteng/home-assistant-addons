#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${LEDGER_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "缺少 Add-on options 文件"
    exit 1
fi

LEDGER_API_TOKEN=$(jq -r '.api_token // ""' "$OPTIONS_FILE")
if [ "${#LEDGER_API_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "请配置至少 32 个字符的内部 API Token"
    exit 1
fi

export LEDGER_API_TOKEN
export LEDGER_WRITER_MODE=$(jq -r '.writer_mode // "read_only"' "$OPTIONS_FILE")
export LEDGER_MAX_REQUEST_BYTES=$(jq -r '.max_request_bytes // 33554432' "$OPTIONS_FILE")
export LEDGER_MAX_ATTACHMENT_BYTES=$(jq -r '.max_attachment_bytes // 20971520' "$OPTIONS_FILE")
export LEDGER_PORTABLE_HISTORY_LIMIT=$(jq -r '.portable_history_limit // 20' "$OPTIONS_FILE")
export LEDGER_DATABASE_PATH="/data/ledger.sqlite3"
export LEDGER_DATA_DIR="/data"
export LEDGER_SHARE_DIR="/share/private/renovation-bookkeeping"

bashio::log.info "启动 Renovation Ledger，writer_mode=${LEDGER_WRITER_MODE}"
exec python3 -m renovation_ledger.main
