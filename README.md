# Kongweiteng Home Assistant Add-ons

Canonical Home Assistant add-on repository for every application maintained and reviewed by Kongweiteng. Home Assistant instances should add this single repository instead of registering separate repositories for each self-maintained application.

## Available add-ons

| Add-on | Purpose | Architectures | Upstream |
| --- | --- | --- | --- |
| [Codex Controller](codex_controller/) | Persistent task controller built on the official Codex app-server, with constrained tool routing | `amd64`, `aarch64` | [openai/codex](https://github.com/openai/codex) |
| [DDNS-GO](ddns_go/) | Update Aliyun DNS and other providers with the current public IP | `amd64`, `aarch64` | [jeessy2/ddns-go](https://github.com/jeessy2/ddns-go) |
| [DrawIO](drawio/) | Browser-based diagram editor through Home Assistant Ingress | `amd64`, `aarch64` | [jgraph/docker-drawio](https://github.com/jgraph/docker-drawio) |
| [ESLink Gas](eslink_gas/) | Read-only multi-account gas balance and meter status through Ingress and MQTT Discovery | `amd64`, `aarch64` | Undocumented ESLink mobile service hall |
| [HA Manager Executor](ha_manager_executor/) | Read-only shadow for the isolated Supervisor manager execution domain | `amd64`, `aarch64` | Self-maintained |
| [HA Operations Broker](ha_operations_broker/) | Passkey authorization broker with a default-off, exact allowlist Add-on restart executor | `amd64`, `aarch64` | Self-maintained |
| [Hermes Agent](hermes_agent/) | Persistent AI agent, dashboard, terminal and API | `amd64`, `aarch64` | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) |
| [Huaxin Water](huaxin_water/) | Read-only multi-account water history, statistics and MQTT Discovery through Ingress | `amd64`, `aarch64` | Undocumented Tianjin Huaxin Water H5 API |
| [Journey Analyzer](journey_analyzer/) | Local HA location collection, journey statistics and authenticated AMap playback | `amd64`, `aarch64` | Self-maintained |
| [Renovation Hub](renovation_hub/) | Independent renovation project, ledger, timeline and media archive with legacy Ledger v1 compatibility | `amd64`, `aarch64` | Self-maintained |
| [Weixin Gateway](weixin_gateway/) | Minimal iLink text/media gateway with one-time owner binding for the current personal Weixin bot identity | `amd64`, `aarch64` | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) protocol reference |
| [WizNote Server](wiznote_server/) | Self-hosted notes with persistent storage, OSS guidance and cold backups | `amd64` | [wiznote/wizserver](https://hub.docker.com/r/wiznote/wizserver) |

## Add this repository to Home Assistant

1. Open **Settings > Apps > Install app**.
2. Open the top-right menu and select **Repositories**.
3. Add `https://github.com/kongweiteng/home-assistant-addons`.
4. Refresh the app store.

Secrets and service credentials must never be committed to this repository.

## Maintenance policy

- Each maintained add-on lives in its own directory with a stable slug.
- Upstream releases are pinned to an explicit version and checksum when external artifacts are downloaded during the build.
- Supported architectures must be declared and build-checked before release.
- Version changes must update the add-on metadata and changelog together.
- Official or community add-ons stay in their upstream repositories unless this repository intentionally takes over maintenance or carries a required patch.
- Runtime passwords, API tokens, cloud credentials and Home Assistant instance data are never committed here.

---

## Hermes Agent

> The self-improving AI agent built by Nous Research. Home Assistant add-on by Wolfram Ravenwolf.

[![Hermes Agent running in Home Assistant](hermes-ha-addon.png)](https://github.com/WolframRavenwolf/hermes-ha-addon/releases/download/v1.0.0/hermes-ha-addon.mp4)

[Hermes Agent](https://hermes-agent.nousresearch.com/) packaged as a [Home Assistant](https://home-assistant.io/) add-on/app. Persistent AI agent with memory, self-improving skills, multi-platform messaging, and a plugin architecture for custom tools.

## Features

- **Persistent memory** -- SQLite FTS5 long-term memory that survives restarts
- **Self-improving skills** -- agent learns and creates new capabilities over time
- **Multi-platform messaging** -- Telegram, Discord, WhatsApp, and more via the gateway
- **MQTT notification bridge** -- optional model-free Home Assistant notifications through the primary Weixin Home Channel
- **Home Assistant plugin research** -- evidence-backed official, Add-on, HACS, HASSbian, and GitHub candidate evaluation without installation
- **Home Assistant health snapshot** -- deterministic disk, Recorder, backup, storage-consumer, and component freshness through explicit read-only HA entities
- **Home Assistant operation approvals** -- opt-in immutable proposals, owner-only model-free commands, TTL, L3 confirmation, and a non-executing audit ledger
- **OpenAI-compatible API** -- connect any chat frontend ([Open WebUI](https://github.com/open-webui/open-webui), [SillyTavern](https://github.com/SillyTavern/SillyTavern), etc.) via `/v1/`
- **Hermes Desktop backend** -- opt-in remote backend for the official Hermes Desktop app on a dedicated port
- **Plugin architecture** -- custom tools, commands, and hooks without forking
- **Self-modifiable source** -- editable install lets the agent read and modify its own code
- **Web dashboard** -- browser-based management UI for config, API keys, sessions, analytics, logs, cron, and skills
- **Persistent web terminal** -- full CLI access via tmux-backed ttyd through the Home Assistant sidebar
- **HTTP + HTTPS** -- direct LAN access with auto-generated TLS certificates
- **Full persistence** -- source code, venv, Homebrew, npm, Go, and all agent data survive add-on updates

## Installation

1. Add this repository to Home Assistant: **Settings > Apps > Install app > ⋮ > Repositories**
2. Paste the repository URL and click **Add**
3. Find **Hermes Agent** in the store and click **Install**
4. Start the add-on and open **Hermes Agent** from the sidebar
5. The setup wizard runs automatically -- configure your model and API keys

## Configuration

Add-on-level options are configured in the Home Assistant UI (Settings > Apps > Hermes Agent > Configuration):

| Option                | Default                                            | Description                                                                     |
| --------------------- | -------------------------------------------------- | ------------------------------------------------------------------------------- |
| `git_url`             | `https://github.com/NousResearch/hermes-agent.git` | Git repository URL (clear to reset to default)                                  |
| `git_ref`             |                                                    | Branch, tag, or commit (empty = repo's default branch)                          |
| `git_token`           |                                                    | Token for private repos + exported as `GITHUB_TOKEN` for gh CLI                 |
| `auto_update`         | `false`                                            | Pull latest changes on restart (preserves local modifications)                  |
| `hass_url`            | `http://homeassistant.local:8123`                  | Home Assistant URL used only when an explicit long-lived token is configured    |
| `homeassistant_token` |                                                    | Optional long-lived token; blank uses the add-on's short-lived Supervisor token |
| `ha_control_allowed_domains` | `light`                                      | Write-enabled domains; only `light` and `switch` are supported                  |
| `ha_control_allowed_entities` | empty                                       | Optional exact entity allowlist; empty permits entities in enabled domains      |
| `ha_health_entities` | empty | Explicit numeric HA entity mapping for disk, Recorder, backup, and storage-consumer metrics |
| `ha_health_status_entities` | empty | Explicit binary-sensor mapping plus the expected `on`/`off` state for critical component health |
| `ha_health_stale_after_seconds` | `300` | Maximum accepted source age before the whole snapshot becomes `stale` |
| `ha_operations_approval_enabled` | `false` | Enable the non-executing proposal/approval protocol on the primary profile |
| `ha_operations_owner_identity_hashes` | empty | SHA-256 hashes of exact `weixin:user_id` owner identities; raw IDs are not stored |
| `ha_operations_proposal_ttl_seconds` | `600` | Approval lifetime; expired action IDs cannot be reused |
| `ha_operations_max_pending` | `20` | Maximum unexpired pending/approved proposals |
| `notification_bridge_enabled` | `false`                                     | Enable the versioned MQTT-to-Weixin notification bridge                         |
| `notification_mqtt_host` | `core-mosquitto`                                  | MQTT broker hostname on the internal app network                                |
| `notification_mqtt_port` | `1883`                                             | MQTT broker port                                                                |
| `notification_mqtt_username` | empty                                         | Dedicated least-privilege MQTT username                                         |
| `notification_mqtt_password` | empty                                         | Password for the dedicated notification MQTT user                               |
| `notification_mqtt_tls` | `false`                                             | Use TLS with broker certificate validation                                      |
| `notification_allowed_audiences` | `owner`                                    | Accepted logical audience aliases; no Weixin IDs are stored in MQTT             |
| `enable_dashboard`    | `false`                                            | Enable web dashboard on direct HTTP/HTTPS ports                                 |
| `enable_terminal`     | `false`                                            | Enable web terminal on direct HTTP/HTTPS ports                                  |
| `enable_api`          | `false`                                            | Enable the OpenAI-compatible API server on direct HTTP/HTTPS ports              |
| `enable_desktop_backend` | `false`                                         | Enable the official Hermes Desktop remote backend on container port 9119        |
| `access_password`     |                                                    | Password for HTTP/HTTPS, API, and Hermes Desktop access (username: `hermes`)    |
| `env_vars`            | `OPENROUTER_API_KEY` (example)                     | Hermes .env variables — written to each profile's `.env` on each start          |
| `hermes_home`         | `.hermes`                                          | Single-profile mode: agent profile directory (relative to ~). Ignored if `profiles` is non-empty |
| `profiles`            | `[]`                                               | Multi-profile mode: list of profile directories run concurrently. First entry is the primary |
| `profiles_base`       | `.hermes/profiles`                                 | Default parent dir for non-dotted profile names. Entries starting with `.` are taken as-is (legacy `.hermes` keeps working). Set to empty to disable the prefix |
| `profile_env_vars`    | `[]`                                               | Per-profile `.env` overrides: each entry is `{profile, name, value}` where `profile` matches a directory in `profiles` |

API keys can be configured in two places: `env_vars` above (convenient, via Home Assistant UI) or each profile's `.env` directly (full list, via terminal or `hermes setup`). Non-empty top-level `env_vars` are written to every profile's `.env` on each start, overriding existing entries. `profile_env_vars` entries layer on top of the top-level set for the profile whose directory matches `profile`.

Home Assistant access uses the add-on's short-lived Supervisor credential by default, so no long-lived HA token is required. The explicit `homeassistant_token` and `hass_url` options remain available for nonstandard deployments. Home Assistant does not provide service-level token permissions, so the add-on replaces upstream Hermes' broad service caller with a restricted implementation: ordinary device states are readable, security-sensitive domains are hidden, every write targets one exact entity, target expansion is rejected, and success is reported only after the entity's real HA state is re-read and verified.

### Home Assistant health snapshot

Hermes registers a read-only `ha_health_snapshot` tool. It reads only the exact entities configured in the add-on options and returns a bounded versioned document containing source timestamps, freshness, disk totals, Recorder size, backup count/size, optional top consumers, and expected-state checks for binary sensors.

The add-on deliberately keeps `homeassistant_api: true` and does not enable `hassio_api`. It therefore does not call Supervisor backup or add-on management endpoints and does not auto-discover entity names. Configure or create suitable HA sensors separately, verify their units and ownership, then map their exact IDs:

```yaml
ha_health_entities:
  - metric: disk_total_bytes
    entity_id: sensor.example_disk_total
  - metric: disk_used_bytes
    entity_id: sensor.example_disk_used
  - metric: disk_free_bytes
    entity_id: sensor.example_disk_free
  - metric: disk_used_percent
    entity_id: sensor.example_disk_used_percent
  - metric: recorder_bytes
    entity_id: sensor.example_recorder_size
  - metric: backup_count
    entity_id: sensor.example_backup_count
  - metric: backup_bytes
    entity_id: sensor.example_backup_size
  - metric: top_consumer
    entity_id: sensor.example_addon_data_size
ha_health_status_entities:
  - entity_id: binary_sensor.hermes_notification_bridge_online
    expected_state: "on"
ha_health_stale_after_seconds: 300
```

Supported numeric metrics are:

| Metric | Expected unit/meaning |
| --- | --- |
| `disk_total_bytes`, `disk_used_bytes`, `disk_free_bytes` | HA sensor with `B`, `KB`, `MB`, `GB`, `TB`, `KiB`, `MiB`, `GiB`, or `TiB` |
| `disk_used_percent` | `%`, from 0 through 100 |
| `recorder_bytes`, `backup_bytes`, `top_consumer` | Same byte units as disk metrics |
| `backup_count` | Non-negative whole number; optional count unit such as `backups` or `items` |

Missing metrics are `null`, never `0`. Invalid units, duplicate singleton mappings, unavailable entities, future timestamps, and stale sources are preserved as explicit quality issues. A snapshot reports `critical` only after a future deterministic P3 threshold policy is implemented; P2 reports data-quality problems as `warning`, `stale`, or `unavailable`.

### MQTT notification bridge

The optional bridge subscribes to `home/notification/v1/request` at QoS 1 and invokes the primary profile with:

```bash
hermes send -q --to weixin --file -
```

This path does not call a model. It validates the request schema, TTL and audience, then applies message-ID idempotency, a deduplication window, source/global rate limits, bounded retries, and a persistent SQLite delivery ledger. Only routing metadata and status are stored; notification bodies and Weixin identities are not written to the ledger. The bridge removes all `NOTIFICATION_*` variables before launching `hermes send`, so its MQTT credentials are not inherited by the Hermes subprocess.

Results are published to `home/notification/v1/result` without retain. The retained `home/notification/v1/status` topic contains only bridge health. MQTT Discovery creates diagnostic entities for bridge connectivity and the latest result. Discovery payloads are retained and are re-published after Home Assistant's `homeassistant/status` birth message.

Use an individual MQTT user for the bridge and restrict it to:

- read: `home/notification/v1/request`, `homeassistant/status`
- write: `home/notification/v1/result`, `home/notification/v1/status`, `homeassistant/binary_sensor/hermes_notification_bridge_online/config`, `homeassistant/sensor/hermes_notification_last_result/config`

Home Assistant's built-in `homeassistant` and `addons` MQTT users must retain their required unrestricted access if broker ACLs are enabled. Keep the bridge disabled until its dedicated MQTT credentials and ACL have been configured. The credential fields remain optional while the bridge is disabled and startup fails closed if it is enabled without both values. Version 1 routes accepted audiences to the primary Weixin Home Channel; an audience alias is not a Weixin account or user ID.

### Home Assistant plugin research Skill

The add-on installs an add-on-managed `home-assistant-plugin-research` Skill into every configured Hermes profile. Natural-language requests to find or compare Home Assistant integrations, Add-ons, HACS repositories, or manual custom components can therefore produce one to three evidence-backed candidates with original links, maintainer and release metadata, compatibility, installation method, permissions, risk, and a deterministic recommendation grade.

This is a research-only capability. HASSbian is used only to discover leads, and every candidate must be confirmed by Home Assistant official documentation, HACS evidence, or the original GitHub repository. The Skill does not install anything, does not call Home Assistant or Supervisor write APIs, and does not use HACS or GitHub write credentials. Webpages, README files, issues, and forum posts are treated as untrusted data and their instructions are never executed.

The bundled normalizer is standard-library-only, performs no network requests, accepts at most three public HTTPS candidates, and rejects private, credentialed, HASSbian-only, or source-mismatched evidence. The installed Skill directory is refreshed on each add-on start and is reserved for add-on management; keep custom skills in a different directory.

### Home Assistant operation approval protocol

The opt-in `ha-operations-approval` plugin creates immutable, non-executing operation proposals. The model can call `ha_create_operation_proposal` with an allowlisted action, a logical target, a non-secret parameter summary, expected changes, validation, rollback, and backup requirement. The plugin derives the minimum risk, generates an action ID and canonical SHA-256 proposal hash, then stores only bounded audit metadata in a profile-local SQLite ledger.

Approval commands are processed as plugin slash commands before model dispatch:

```text
/ha-approve OPS-YYYYMMDD-XXXXXXXXXXXX
/ha-confirm OPS-YYYYMMDD-XXXXXXXXXXXX XXXXXXXX
/ha-cancel OPS-YYYYMMDD-XXXXXXXXXXXX
```

Only the primary profile can load the managed plugin. Approval requires a Weixin private chat whose `sha256("weixin:" + user_id)` value appears in `ha_operations_owner_identity_hashes`. Raw Weixin identifiers are not stored in add-on options or the audit ledger. L3 proposals require the same owner to complete a second challenge within the proposal TTL. Group messages, other platforms, natural-language approval, expired IDs, duplicate execution attempts, secret-like fields, credentialed URLs, and risk downgrades are rejected.

This protocol does not install, configure, restart, delete, or otherwise change Home Assistant. Hermes still has no Supervisor management permission. A separately isolated Operations Broker must re-validate the action schema, proposal hash, owner decision, TTL, and idempotency before it can become the sole production writer in a later phase.

### Running multiple profiles concurrently

Set `profiles` to run several Hermes instances in the same add-on. Non-dotted names are placed under `profiles_base` (default `.hermes/profiles`) to match upstream's [profile layout](https://hermes-agent.nousresearch.com/docs/user-guide/profiles); entries starting with `.` are taken as-is. Per-profile env overrides live in a flat `profile_env_vars` list (Home Assistant Supervisor only allows nested objects two levels deep):

```yaml
profiles:
  - .hermes              # primary, kept as-is (leading "."): /config/.hermes
  - amy                  # bare name, prefixed by profiles_base: /config/.hermes/profiles/amy
  - bob                  # same: /config/.hermes/profiles/bob
profile_env_vars:
  # `profile` matches the entry above exactly (use the string you put in `profiles`).
  - profile: amy
    name: OPENROUTER_API_KEY
    value: amy-only-key
  - profile: amy
    name: SOME_AMY_VAR
    value: amy-special
```

A single shared install at `~/.hermes/hermes-agent` (clone + venv) backs every profile — only the per-profile `.env`, `config.yaml`, `SOUL.md`, sessions, memories, and logs live under each profile's directory.

The first entry is the **primary** — it keeps the existing root URLs (`/hermes/`, `/dashboard/`, `/terminal/`, `/v1/`). Each additional profile is exposed under `/profile/<name>/...`. Per-profile ports allocate from a base + index (`8642`, `49269`, `49369`, `49469`).

**Upgrade note:** If you already used bare profile names with earlier multi-profile add-on versions, existing flat directories such as `/config/amy` are preserved automatically when the new `.hermes/profiles/amy` directory does not exist yet. To keep flat paths intentionally, set `profiles_base` to an empty string. To adopt the upstream-style layout, move the profile data to `/config/.hermes/profiles/<name>`.

**Note:** Values added via `env_vars` are not removed or reset from `.env` when cleared or removed in the Home Assistant UI -- edit each profile's `.env` directly to remove them.

Hermes-internal configuration (model, platforms, memory, tools) is managed via the terminal:

```bash
hermes setup          # Interactive first-time setup
hermes config edit    # Edit config directly
hermes doctor         # Diagnostics and dependency check
hermes gateway setup  # Configure messaging platforms
```

## Access

The add-on is accessible via the **Home Assistant Sidebar** (landing page with embedded terminal, mode switching, and status display) and, optionally, via direct URLs. Replace `homeassistant.local` with your Home Assistant hostname or IP.

Direct HTTP/HTTPS access requires `enable_dashboard` (**Enable Web Dashboard**), `enable_terminal` (**Enable Web Terminal**), and/or `enable_api` (**Enable API Server**) in the add-on configuration. Set an **Access Password** to secure these ports (username: `hermes`). The separate Hermes Desktop backend requires `enable_desktop_backend`, an access password, and an explicit host-port mapping for container port 9119 under **Network**.

### Web Terminal & Dashboard

| URL                                            | Description                                                              |
| ---------------------------------------------- | ------------------------------------------------------------------------ |
| `https://homeassistant.local:8443/hermes/`     | Hermes Agent (starts hermes, crash drops to shell)                       |
| `https://homeassistant.local:8443/dashboard/`  | Web dashboard (config, API keys, sessions, analytics, logs)              |
| `https://homeassistant.local:8443/terminal/`   | Shell terminal (non-login shell -- plain shell, hermes not auto-started) |
| `https://homeassistant.local:8443/cert/ca.crt` | CA certificate download (for trusting self-signed HTTPS)                 |

### Hermes Desktop remote backend

The official Hermes Desktop app can use the add-on's primary Hermes installation as a remote backend:

1. Set a strong `access_password`.
2. Enable `enable_desktop_backend`.
3. Under the add-on's **Network** settings, map container port `9119` to the host port you want to use (normally `9119`).
4. In Hermes Desktop, add `http://homeassistant.local:9119` as the remote backend URL, replacing the hostname and host port when necessary.
5. Log in with username `hermes` and the configured access password.

This opt-in endpoint provides powerful, full agent control. The add-on does not claim process or secret isolation from authenticated agent activity. Enable it only when you accept that risk, and expose it only on a trusted LAN, VPN, or Tailscale path. Do not publish port 9119 directly to the internet.

The Desktop backend derives and pins Hermes' machine root from the primary `HERMES_HOME` before Hermes loads its configuration. Standard Hermes profiles below that machine root remain available through Hermes Desktop; legacy flat profiles outside that root are not promised through this endpoint.

### OpenAI-compatible API

Connect [Open WebUI](https://github.com/open-webui/open-webui), [SillyTavern](https://github.com/SillyTavern/SillyTavern), etc.

OpenAI-compatible API access requires `enable_api` (**Enable API Server**) in the add-on configuration. The **Access Password** doubles as the server API key.

| URL / Endpoint                                                | Method | Description                                       |
| ------------------------------------------------------------- | ------ | ------------------------------------------------- |
| `https://homeassistant.local:8443/v1/chat/completions`        | POST   | Chat Completions (stateless)                      |
| `https://homeassistant.local:8443/v1/responses`               | POST   | Responses API (stateful via previous_response_id) |
| `https://homeassistant.local:8443/v1/responses/{response_id}` | GET    | Retrieve a stored response                        |
| `https://homeassistant.local:8443/v1/responses/{response_id}` | DELETE | Delete a stored response                          |
| `https://homeassistant.local:8443/v1/models`                  | GET    | List available models                             |
| `https://homeassistant.local:8443/health`                     | GET    | Health check                                      |

### Ports

All direct ports are configurable in the Home Assistant add-on network settings. Use the HTTPS port (8443) with an access password for secure browser access. The HTTP port (8080) is intended for TLS-terminating reverse proxies and disabled by default. Port 9119 is the opt-in Hermes Desktop backend and is also unmapped by default.

| Port     | Description                                          |
| -------- | ---------------------------------------------------- |
| **8080** | HTTP access (all URLs above, replace 8443 with 8080) |
| **8443** | HTTPS access (TLS with self-signed cert)             |
| **9119** | Hermes Desktop remote backend (plain HTTP; trusted LAN/VPN/Tailscale only) |

### SSH

Via Home Assistant host + docker exec, no SSH server in container required. Port 22222 is the default for the Advanced SSH & Web Terminal add-on (adjust if yours differs).

```bash
# Plain shell (new session, not shared with web terminal)
ssh -p 22222 -t root@homeassistant.local "docker exec -it \$(docker ps -qf name=hermes_agent) bash"

# Hermes (shared tmux session — same as Home Assistant sidebar "Hermes" tab)
# Replace <profile> with the sanitized profile name (e.g. "hermes" for the primary `.hermes`, "amy" for `amy`).
ssh -p 22222 -t root@homeassistant.local "docker exec -it \$(docker ps -qf name=hermes_agent) tmux -L hermes-<profile> -u new -A -s hermes-<profile> /usr/local/bin/start-hermes"

# Terminal (shared tmux session — same as Home Assistant sidebar "Terminal" tab)
ssh -p 22222 -t root@homeassistant.local "docker exec -it \$(docker ps -qf name=hermes_agent) tmux -L terminal-<profile> -u new -A -s terminal-<profile> bash"

# Copy files (e.g. upload a custom SOUL.md — works even when add-on is stopped)
scp -P 22222 SOUL.md "root@homeassistant.local:/mnt/data/supervisor/addon_configs/*hermes_agent/.hermes/"
```

### TLS Certificates

On first start, self-signed certificates are auto-generated in `~/.certs/`. To trust the HTTPS connection and avoid browser warnings, install the CA certificate on your devices:

1. Click **CA Cert** in the add-on titlebar (or download from `/cert/ca.crt`)
2. Install the certificate:
   - **Windows**: Double-click the .crt file → Install Certificate → Local Machine → Trusted Root Certification Authorities
   - **macOS**: Double-click → Keychain Access → set to "Always Trust"
   - **Android**: Settings → Security → Install certificate → CA certificate → select the file
   - **iOS**: Open the .crt file → Install Profile → Settings → General → About → Certificate Trust Settings → enable
   - **Linux**: Copy to `/usr/local/share/ca-certificates/` and run `sudo update-ca-certificates`

To use your own certificates instead of self-signed:

1. Stop the add-on
2. Replace `~/.certs/server.crt` and `~/.certs/server.key` with your own
3. Optionally replace `~/.certs/ca.crt` if you have a custom CA
4. Start the add-on

The add-on will use existing certificates and never overwrite them.

## Security Model

Authentication layers differ by access path:

- **Home Assistant Ingress** (sidebar): protected by Home Assistant's own session auth. All services — Hermes, Terminal, Dashboard — are reachable once you're logged in to HA.
- **Direct HTTP/HTTPS Ports** (8080/8443): two-layer auth protects the web UIs.
  1. **Basic Auth** (username `hermes`, password = `access_password`) gates the landing page, Terminal, and Dashboard HTML.
  2. **Session Token** (ephemeral, rotates on every add-on restart) gates dashboard API calls. The token is injected into the dashboard HTML on load — only clients who successfully loaded the page via Basic Auth ever see it. Requests to `/dashboard/api/*` without a matching Bearer token return 401. Only `/dashboard/api/status` is public (it mirrors Hermes' own whitelist and powers the landing page health indicator). If the dashboard process is restarted without restarting the add-on, the nginx-side token cache goes stale — restart the add-on to re-sync.
- **OpenAI-compatible API** (`/v1/*`): Bearer token authentication. The `access_password` doubles as the API key, passed as `Authorization: Bearer <api-key>`.
- **Hermes Desktop backend** (`:9119` when enabled and mapped): Hermes Basic-auth login using username `hermes` and `access_password`. This endpoint exposes the full Desktop backend contract, including chat, WebSockets, PTY, events, profiles, and agent control. It is disabled and unmapped by default. Enabling it is an explicit risk decision; keep it on a trusted LAN/VPN/Tailscale path and do not expose it directly to the internet.
- **Home Assistant device control**: the add-on requests Core API access and converts its short-lived Supervisor credential into the `HASS_TOKEN` expected by Hermes. Read tools exclude locks, alarms, cameras, people, device trackers, automations, scripts, scenes, and other sensitive/action domains. Write tools default to `light.turn_on` and `light.turn_off`; `switch` must be explicitly enabled and also requires an exact entity allowlist. Calls require one exact entity, accept only restricted parameters, and verify the resulting HA state before returning success. Use `ha_control_allowed_entities` when control must be narrower than the enabled domain.
- **Home Assistant health snapshot**: the add-on reads exact configured `sensor` and `binary_sensor` entity IDs through the Core API. The compact base64 runtime configuration prevents shell interpolation, raw HA attributes are not returned, and no Supervisor management permission or filesystem scan is added.
- **MQTT notification bridge**: disabled by default. When enabled, it uses separate broker credentials, subscribes only to the versioned request contract, rejects unknown audiences, persists no notification body, and sends through `hermes send` without model execution. Request/result messages are never retained.

If you expose direct ports to the internet, place a network-perimeter gate (firewall, VPN, reverse proxy with stronger auth) in front — Basic Auth alone is not brute-force resistant.

## Architecture

Six service families in a Debian Bookworm container:

1. **Hermes Gateway** (`hermes gateway run`) -- persistent AI agent daemon with OpenAI-compatible API server and messaging platform connectors. Logs visible in the Home Assistant add-on log and in `~/.hermes/logs/gateway.log`.
2. **Hermes Dashboard** (`hermes dashboard`) -- browser-based management UI (FastAPI + React) for config, API keys, sessions, analytics, logs, cron jobs, and skills.
3. **ttyd** (×2 per profile) -- web terminals backed by persistent tmux sessions (`hermes-<name>` + `terminal-<name>`)
4. **nginx** -- HTTP, HTTPS, and Home Assistant ingress proxy routing to dashboard + terminal + API. Multi-profile setups serve `/profile/<name>/...` for non-primary profiles.
5. **Hermes Desktop backend** (`hermes serve`, optional) -- official root-level HTTP/WebSocket backend on container port 9119, using the primary Hermes machine root and Basic-auth login.
6. **MQTT notification bridge** (optional) -- a standalone Python process with MQTT v5 persistent sessions, manual QoS acknowledgements, SQLite idempotency, MQTT Discovery, and deterministic Weixin delivery.

### Shell Environment

The Hermes tab uses a dedicated `start-hermes` wrapper (sources .bashrc, starts hermes, fallback shell on error). The Terminal tab provides a plain shell with all paths configured.

| File                | Persistent? | Purpose                                         |
| ------------------- | ----------- | ----------------------------------------------- |
| `~/.bashrc`         | Yes         | Sources .hermes_profile + .env, prompt, aliases |
| `~/.hermes_profile` | Regenerated | Env vars, PATH, tokens (from add-on config)     |
| `~/.profile`        | Yes         | Sources .bashrc (login shell init)              |
| `~/.tmux.conf`      | Yes         | Terminal config (mouse scroll, history)         |

### Persistent Storage

Inside the add-on container, `~` is `/config`. That path is the add-on's private `addon_config` mount, not the normal Home Assistant Core `/config` folder. It survives add-on updates and is included in Home Assistant backups.

From the HAOS host or Samba, look for the `addon_configs` share/folder. The host-side path usually looks like this:

```text
/mnt/data/supervisor/addon_configs/<repo_or_slug>_hermes_agent/
```

For example, a locally installed/custom repository may appear as something like:

```text
/mnt/data/supervisor/addon_configs/a0b1c2d3_hermes_agent/
```

The exact prefix is installation-specific, but the important bit is: use `addon_configs`, not the regular Home Assistant Core config folder.

The default single-profile layout after a successful first start is:

```text
~ (/config inside the add-on, addon_configs/..._hermes_agent on the host)
├── .certs/                    # TLS certificates (auto-generated or custom)
├── .go/                       # Go workspace
├── .hermes/                   # Primary HERMES_HOME (official installer layout)
│   ├── hermes-agent/          # Git clone (source code, agent-modifiable)
│   │   └── venv/              # Python venv (editable install)
│   ├── logs/                  # Gateway logs
│   ├── notification-bridge/   # Delivery ledger + notification bridge log state
│   ├── memories/              # Long-term memory (MEMORY.md, USER.md)
│   ├── sessions/              # Conversation state
│   ├── skills/                # Auto-created + installed skills
│   ├── .env                   # API keys (chmod 600)
│   ├── SOUL.md                # Agent personality
│   ├── config.yaml            # Hermes config (model, platforms, tools)
│   └── state.db               # SQLite FTS5 state
├── .linuxbrew/                # Homebrew
├── .npm-global/               # npm global packages
├── .bash_aliases              # Custom aliases and functions (optional, user-created)
├── .bashrc                    # Shell config
├── .hermes_install            # Shared install marker for the Hermes clone + venv
├── .hermes_profile            # Env vars + PATH (regenerated)
├── .profile                   # Sources .bashrc (login shell init)
└── .tmux.conf                 # tmux config

/media/                        # Home Assistant media directory (shared, visible in Home Assistant media browser)
/share/                        # Home Assistant shared directory (shared between all add-ons)
```

Directories are created lazily during startup. If `/config/.hermes/hermes-agent` is missing inside the add-on terminal, the add-on likely has not completed the clone/install step yet; check the add-on log around the `[run] [hermes] Cloning Hermes Agent`, `Creating venv`, and `Installing Hermes` lines.

### Container Toolchain

Pre-installed at build time:

- **Languages**: Go 1.26, Node.js 22, Python 3.11
- **Browser**: Chromium, agent-browser
- **Dev tools**: bat, bc, fd-find, gh (GitHub CLI), git, htop, jq, moreutils, nano, ripgrep, tree, vim, yq
- **Graphics**: ghostscript, imagemagick
- **Media**: ffmpeg
- **Networking**: curl, dnsutils, netcat, openssh-client, ping, wget
- **Package managers**: go, Homebrew (Linuxbrew), npm, uv
- **System**: bash-completion, command-not-found, rsync, sqlite3, tmux, unzip/zip

### Supported Architectures

- `amd64`
- `aarch64`

## Changelog

Release notes for Home Assistant update screens live in [`hermes_agent/CHANGELOG.md`](hermes_agent/CHANGELOG.md). GitHub Releases carry the same user-facing release notes for tagged versions.

## License

This Home Assistant add-on/app is [MIT licensed](LICENSE). Hermes Agent itself is also [MIT licensed](https://github.com/NousResearch/hermes-agent/blob/main/LICENSE).

---

Copyright (c) 2026 Wolfram Ravenwolf
