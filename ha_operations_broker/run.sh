#!/command/with-contenv bashio
set -euo pipefail

OPTIONS_FILE="${BROKER_OPTIONS_FILE:-/data/options.json}"
if [ ! -f "$OPTIONS_FILE" ]; then
    bashio::log.fatal "Options file is missing"
    exit 1
fi

BROKER_API_TOKEN=$(jq -r '.broker_api_token // ""' "$OPTIONS_FILE")
BROKER_RECOVERY_API_TOKEN=$(jq -r '.recovery_api_token // ""' "$OPTIONS_FILE")
BROKER_BACKUP_EVIDENCE_API_TOKEN=$(jq -r '.backup_evidence_api_token // ""' "$OPTIONS_FILE")
BROKER_OWNER_HASHES=$(jq -r '(.trusted_owner_identity_hashes // []) | map(select(length > 0)) | join(",")' "$OPTIONS_FILE")
BROKER_MAX_REQUEST_BYTES=$(jq -r '.max_request_bytes // 32768' "$OPTIONS_FILE")
BROKER_SUPERVISOR_TIMEOUT_SECONDS=$(jq -r '.supervisor_timeout_seconds // 5' "$OPTIONS_FILE")
BROKER_WEBAUTHN_RP_ID=$(jq -r '.webauthn_rp_id // ""' "$OPTIONS_FILE")
BROKER_WEBAUTHN_ALLOWED_ORIGINS=$(jq -r '(.webauthn_allowed_origins // []) | map(select(length > 0)) | join(",")' "$OPTIONS_FILE")
BROKER_PASSKEY_ENROLLMENT_TOKEN=$(jq -r '.passkey_enrollment_token // ""' "$OPTIONS_FILE")
BROKER_PASSKEY_CHALLENGE_TTL_SECONDS=$(jq -r '.passkey_challenge_ttl_seconds // 180' "$OPTIONS_FILE")
BROKER_MAX_PASSKEYS=$(jq -r '.max_passkeys // 8' "$OPTIONS_FILE")
BROKER_MAX_PENDING_FLOWS=$(jq -r '.max_pending_passkey_flows // 100' "$OPTIONS_FILE")
BROKER_PROPOSAL_TTL_SECONDS=$(jq -r '.proposal_ttl_seconds // 600' "$OPTIONS_FILE")
BROKER_POLICY_EPOCH=$(jq -r '.policy_epoch // 1' "$OPTIONS_FILE")
BROKER_POLICY_HASH=$(jq -r '.policy_hash // "sha256:c408de9e17185d22a780c6e5bca8cb3ad7cb092f46cdfe9a35fe1ecd6a3719b8"' "$OPTIONS_FILE")
BROKER_ADAPTER_VERSION=$(jq -r '.adapter_version // "manager-restart-v1"' "$OPTIONS_FILE")
BROKER_ADAPTER_SCHEMA_VERSION=$(jq -r '.adapter_schema_version // 1' "$OPTIONS_FILE")
BROKER_LEASE_TTL_SECONDS=$(jq -r '.lease_ttl_seconds // 30' "$OPTIONS_FILE")
BROKER_MANAGER_SHADOW_ENABLED=$(jq -r 'if .manager_shadow_enabled == true then "true" else "false" end' "$OPTIONS_FILE")
BROKER_MANAGER_EXECUTOR_BASE_URL=$(jq -r '.manager_executor_base_url // ""' "$OPTIONS_FILE")
BROKER_MANAGER_EXECUTOR_API_TOKEN=$(jq -r '.manager_executor_api_token // ""' "$OPTIONS_FILE")
BROKER_EXECUTION_ENABLED=$(jq -r 'if .execution_enabled == true then "true" else "false" end' "$OPTIONS_FILE")
BROKER_ENABLED_ACTIONS=$(jq -r '(.enabled_actions // []) | map(select(length > 0)) | join(",")' "$OPTIONS_FILE")
BROKER_RESTART_ADDON_ALLOWLIST=$(jq -r '(.restart_addon_allowlist // []) | map(select(length > 0)) | join(",")' "$OPTIONS_FILE")

if [ "${#BROKER_API_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "Configure a broker API token with at least 32 characters"
    exit 1
fi
if [ "${#BROKER_RECOVERY_API_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "Configure a recovery API token with at least 32 characters"
    exit 1
fi
if [ "${#BROKER_BACKUP_EVIDENCE_API_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "Configure a backup evidence API token with at least 32 characters"
    exit 1
fi
if [ "$BROKER_API_TOKEN" = "$BROKER_RECOVERY_API_TOKEN" ] || \
   [ "$BROKER_API_TOKEN" = "$BROKER_BACKUP_EVIDENCE_API_TOKEN" ] || \
   [ "$BROKER_RECOVERY_API_TOKEN" = "$BROKER_BACKUP_EVIDENCE_API_TOKEN" ]; then
    bashio::log.fatal "Broker, recovery and backup evidence API tokens must be different"
    exit 1
fi
if [ -z "$BROKER_WEBAUTHN_RP_ID" ] || [ -z "$BROKER_WEBAUTHN_ALLOWED_ORIGINS" ]; then
    bashio::log.fatal "Configure an exact WebAuthn RP ID and at least one HTTPS origin"
    exit 1
fi
if [ -n "$BROKER_PASSKEY_ENROLLMENT_TOKEN" ] && [ "${#BROKER_PASSKEY_ENROLLMENT_TOKEN}" -lt 32 ]; then
    bashio::log.fatal "Passkey enrollment token must be empty or at least 32 characters"
    exit 1
fi
if [ -z "${SUPERVISOR_TOKEN:-}" ]; then
    bashio::log.fatal "Home Assistant Supervisor credential is unavailable"
    exit 1
fi
if [ -n "$BROKER_ENABLED_ACTIONS" ] && [ "$BROKER_ENABLED_ACTIONS" != "restart_addon" ]; then
    bashio::log.fatal "Only restart_addon can appear in enabled_actions"
    exit 1
fi
if [ "$BROKER_EXECUTION_ENABLED" = "true" ]; then
    if [ "$BROKER_ENABLED_ACTIONS" != "restart_addon" ]; then
        bashio::log.fatal "Execution requires enabled_actions to contain only restart_addon"
        exit 1
    fi
    if [ -z "$BROKER_RESTART_ADDON_ALLOWLIST" ]; then
        bashio::log.fatal "Execution requires at least one exact restart_addon slug"
        exit 1
    fi
fi
if [ "$BROKER_MANAGER_SHADOW_ENABLED" = "true" ]; then
    if [ -z "$BROKER_MANAGER_EXECUTOR_BASE_URL" ] || [ "${#BROKER_MANAGER_EXECUTOR_API_TOKEN}" -lt 32 ]; then
        bashio::log.fatal "Manager shadow requires an internal URL and a token with at least 32 characters"
        exit 1
    fi
fi

export BROKER_API_TOKEN
export BROKER_RECOVERY_API_TOKEN
export BROKER_BACKUP_EVIDENCE_API_TOKEN
export BROKER_OWNER_HASHES
export BROKER_MAX_REQUEST_BYTES
export BROKER_SUPERVISOR_TIMEOUT_SECONDS
export BROKER_WEBAUTHN_RP_ID
export BROKER_WEBAUTHN_ALLOWED_ORIGINS
export BROKER_PASSKEY_ENROLLMENT_TOKEN
export BROKER_PASSKEY_CHALLENGE_TTL_SECONDS
export BROKER_MAX_PASSKEYS
export BROKER_MAX_PENDING_FLOWS
export BROKER_PROPOSAL_TTL_SECONDS
export BROKER_POLICY_EPOCH
export BROKER_POLICY_HASH
export BROKER_ADAPTER_VERSION
export BROKER_ADAPTER_SCHEMA_VERSION
export BROKER_LEASE_TTL_SECONDS
export BROKER_MANAGER_SHADOW_ENABLED
export BROKER_MANAGER_EXECUTOR_BASE_URL
export BROKER_MANAGER_EXECUTOR_API_TOKEN
export BROKER_EXECUTION_ENABLED
export BROKER_ENABLED_ACTIONS
export BROKER_RESTART_ADDON_ALLOWLIST
export BROKER_AUTHORIZATION_DATABASE="/data/authorization/passkeys.sqlite3"
export BROKER_SUPERVISOR_BASE_URL="http://supervisor"
export BROKER_SUPERVISOR_TOKEN="$SUPERVISOR_TOKEN"
unset SUPERVISOR_TOKEN

bashio::log.info "Starting HA Operations Broker; write execution remains controlled by explicit options"
exec /opt/ha-operations-broker/venv/bin/python -m operations_broker.main
