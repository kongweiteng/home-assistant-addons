# Changelog

## 0.1.2

- Probe the optional Supervisor MQTT service without emitting a false startup
  error when an external broker relies on Home Assistant's MQTT integration.

## 0.1.1

- Fall back to Home Assistant's authenticated `mqtt.publish` service when an
  external broker such as EMQX does not register a Supervisor `mqtt` service.
- Preserve retained MQTT Discovery, aggregate-only payloads and stable entity
  IDs without requiring broker credentials in Journey Analyzer options.

## 0.1.0

- Add explicit Home Assistant location-entity collection and bounded Recorder backfill.
- Add deterministic journey segmentation and SQLite persistence.
- Add privacy-safe MQTT Discovery statistics and health entities.
- Add authenticated Ingress API and optional AMap WGS84-to-GCJ-02 journey playback.
- Add cold-backup, no-host-port and no-third-party-upload defaults.
