# Journey Analyzer

Journey Analyzer is a local-first Home Assistant app for selected `person` or
`device_tracker` location entities. It reads Home Assistant Core through the
short-lived Supervisor credential, stores WGS84 points in `/data/journeys.db`,
segments journeys, publishes aggregate MQTT Discovery sensors, and exposes
read-only journey details through authenticated Home Assistant Ingress.

The app does not read a personal AMap account, scrape AMap history, upload raw
tracks, expose a host port, or put coordinate arrays in MQTT or HA attributes.

## Features

- Recorder history backfill plus current-state polling for an explicit entity allowlist.
- Accuracy, duplicate timestamp, `0,0`, time-gap and impossible-speed filtering.
- Local SQLite persistence with a configurable raw-point retention period.
- Today, 7-day, 30-day and last-journey Home Assistant statistics.
- Truthful `no_data`, `insufficient`, `stale`, `degraded`, `good` and `error` quality states.
- Ingress-only bounded journey list, statistics and track-detail APIs.
- Optional AMap Web JS playback with WGS84 to GCJ-02 conversion, satellite tiles and live traffic.
- Cold HA backups so the SQLite database is consistent in a backup archive.

## Install

1. Add `https://github.com/kongweiteng/home-assistant-addons` to the Home Assistant app store.
2. Install **Journey Analyzer**.
3. Configure exactly the source entities you want collected. Avoid selecting a
   `person` and its mirrored `device_tracker` together, because that would count
   the same movement twice.
4. Start the app and open it from the Home Assistant sidebar.

See [DOCS.md](DOCS.md) for configuration, API and recovery details.

## Privacy boundary

Raw coordinates are sensitive personal behavior data. They stay inside the
app's `/data` directory and are returned only from the authenticated Ingress
detail endpoint. MQTT contains aggregate numbers and health states only. AMap
receives browser-side coordinates only when an administrator explicitly
configures the optional Web JS key and security code and opens a journey.
