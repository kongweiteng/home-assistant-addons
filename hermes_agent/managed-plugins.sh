#!/bin/bash
# shellcheck shell=bash

MANAGED_APPROVAL_PLUGIN="ha-operations-approval"
MANAGED_APPROVAL_MARKER=".managed-by-hermes-addon"

managed_plugins_install() {
    local python_bin="$1"
    local source_dir=""
    local candidate
    for candidate in \
        "/usr/local/share/hermes-addon/plugins/$MANAGED_APPROVAL_PLUGIN" \
        "$(dirname "${BASH_SOURCE[0]}")/bundled_plugins/$MANAGED_APPROVAL_PLUGIN"; do
        if [ -d "$candidate" ]; then
            source_dir="$candidate"
            break
        fi
    done
    if [ -z "$source_dir" ] || [ ! -f "$source_dir/plugin.yaml" ] || [ ! -f "$source_dir/__init__.py" ]; then
        echo "[run] FATAL: managed HA operations approval plugin source is incomplete"
        return 1
    fi
    if [ "$HA_OPERATIONS_APPROVAL_ENABLED" = "true" ] && [ -z "$HA_OPERATIONS_OWNER_IDENTITY_HASHES" ]; then
        echo "[run] FATAL: HA operations approval is enabled without an owner identity hash"
        return 1
    fi

    local i home name plugins_dir target stage command
    for i in "${!PROFILE_HOMES[@]}"; do
        home="${PROFILE_HOMES[$i]}"
        name="${PROFILE_NAMES[$i]}"
        plugins_dir="$home/plugins"
        target="$plugins_dir/$MANAGED_APPROVAL_PLUGIN"
        mkdir -p "$plugins_dir"
        if [ -e "$target" ] && [ ! -f "$target/$MANAGED_APPROVAL_MARKER" ]; then
            echo "[run] FATAL: [$name] unmanaged plugin conflicts with reserved $MANAGED_APPROVAL_PLUGIN directory"
            return 1
        fi
        stage="$(mktemp -d "$plugins_dir/.${MANAGED_APPROVAL_PLUGIN}.XXXXXX")"
        cp -R "$source_dir/." "$stage/"
        : > "$stage/$MANAGED_APPROVAL_MARKER"
        find "$stage" -type d -exec chmod 0555 {} +
        find "$stage" -type f -exec chmod 0444 {} +
        if [ -e "$target" ]; then
            rm -rf "$target"
        fi
        mv "$stage" "$target"

        if [ "$i" -eq 0 ] && [ "$HA_OPERATIONS_APPROVAL_ENABLED" = "true" ]; then
            command="enable"
            HERMES_HOME="$home" "$python_bin" -c \
                'from hermes_cli.plugins_cmd import cmd_enable; cmd_enable("ha-operations-approval", allow_tool_override=False)'
        else
            command="disable"
            HERMES_HOME="$home" "$python_bin" -c \
                'from hermes_cli.plugins_cmd import cmd_disable; cmd_disable("ha-operations-approval")'
        fi
        echo "[run] [$name] Managed HA operations approval plugin installed ($command)"
    done
}
