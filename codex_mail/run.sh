#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${CODEX_MAIL_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "Options file is missing"
    exit 1
fi

export CODEX_MAIL_OPTIONS_FILE="$OPTIONS_FILE"
export CODEX_MAIL_DATABASE="${CODEX_MAIL_DATABASE:-/data/mail.sqlite3}"

bashio::log.info "Starting bounded mail provider"
exec python3 -m codex_mail.main
