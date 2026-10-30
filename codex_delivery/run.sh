#!/command/with-contenv bashio
set -euo pipefail
umask 077

export CODEX_DELIVERY_OPTIONS_FILE="${CODEX_DELIVERY_OPTIONS_FILE:-/data/options.json}"
export CODEX_DELIVERY_STATE_ROOT="${CODEX_DELIVERY_STATE_ROOT:-/data/state}"
exec python3 -m codex_delivery.main
