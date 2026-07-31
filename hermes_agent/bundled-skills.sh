#!/usr/bin/env bash
# Install add-on-managed Hermes skills into every configured profile.

bundled_skills_install() {
    local source_root="${BUNDLED_SKILLS_DIR:-/usr/local/share/hermes-addon/skills}"
    local skill_name="home-assistant-plugin-research"
    local source="$source_root/$skill_name"
    local i home name target

    if [ ! -f "$source/SKILL.md" ] || [ ! -f "$source/scripts/normalize_candidates.py" ]; then
        echo "[run] FATAL: bundled Home Assistant plugin research skill is incomplete" >&2
        return 1
    fi

    for i in "${!PROFILE_DIRS[@]}"; do
        home="${PROFILE_HOMES[$i]}"
        name="${PROFILE_NAMES[$i]}"
        target="$home/skills/$skill_name"
        mkdir -p "$target"
        rsync -a --delete "$source/" "$target/"
        find "$target" -type d -exec chmod 0555 {} +
        find "$target" -type f -exec chmod 0444 {} +
        chmod 0555 "$target/scripts/normalize_candidates.py"
        echo "[run] [$name] Managed Home Assistant plugin research skill installed"
    done
}
