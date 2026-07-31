# Journey Analyzer documentation

## Configuration

| Option | Default | Purpose |
| --- | --- | --- |
| `entity_ids` | empty | Exact `person.*` or `device_tracker.*` sources to collect; at least one is required. |
| `poll_interval_s` | `60` | Seconds between collection cycles. |
| `initial_history_hours` | `24` | Recorder history requested on first collection. |
| `analysis_window_hours` | `72` | Bounded window recalculated after late or corrected points. |
| `stale_after_s` | `900` | Age after which the latest observation is reported as stale. |
| `retention_days` | `90` | Raw WGS84 point retention. Journey summaries remain after old raw points are removed. |
| `timezone` | `Asia/Shanghai` | Calendar boundary used for today, 7-day and 30-day statistics. |
| `max_accuracy_m` | `100` | Reject a point whose accuracy radius is worse than this. |
| `max_gap_s` | `900` | Split tracks instead of drawing a straight line across a longer gap. |
| `stop_radius_m` | `80` | Radius used to detect a stay. |
| `stop_min_duration_s` | `600` | Minimum stay duration. |
| `min_journey_distance_m` | `200` | Minimum accepted journey length. |
| `min_journey_duration_s` | `120` | Minimum accepted journey duration. |
| `max_speed_mps` | `100` | Reject an impossible adjacent segment. |
| `amap_web_key` | empty | Optional AMap Web JS key used only by the Ingress page. |
| `amap_security_code` | empty | Optional AMap Web JS security code used only by the Ingress page. |

The app fails closed when `entity_ids` is empty. Entity IDs, coordinates, keys,
tokens and broker credentials must not be committed to this repository.

## Home Assistant entities

MQTT Discovery creates:

- `sensor.journey_analyzer_today_trip_count`
- `sensor.journey_analyzer_today_distance`
- `sensor.journey_analyzer_today_duration`
- `sensor.journey_analyzer_7d_distance`
- `sensor.journey_analyzer_30d_distance`
- `sensor.journey_analyzer_last_trip_distance`
- `sensor.journey_analyzer_last_trip_duration`
- `sensor.journey_analyzer_location_quality`
- `binary_sensor.journey_analyzer_available`

If Supervisor exposes a provider-managed `mqtt` service, the app publishes
directly with its short-lived service credentials. Brokers such as EMQX may be
configured in Home Assistant without registering that Supervisor service; in
that case the app calls Home Assistant's authenticated `mqtt.publish` action.
Both paths use the same retained Discovery topics and aggregate-only payloads.
Journey Analyzer does not need broker credentials in its options.

When no valid location has ever been observed, numeric sensors publish
`unknown`, not a fabricated zero. A valid location with no qualifying journey
may truthfully produce zero for current-period totals and `insufficient` quality.

## Ingress API

- `GET /health`
- `GET /api/v1/journeys?entity_id=&start=&end=&limit=&offset=`
- `GET /api/v1/journeys/{journey_id}`
- `GET /api/v1/stats?entity_id=&period=1|7|30`

The list endpoint returns summaries without coordinates. Only the single
journey detail endpoint returns up to 2,000 WGS84 points. Pagination is capped
at 100 rows and offset 10,000. No host port is mapped, so these endpoints are
intended to be reached through authenticated Home Assistant Ingress only.

## AMap display

When both AMap values are configured, the Ingress page loads AMap Web JS in the
administrator's browser. It converts stored WGS84 points to GCJ-02 at the
display boundary, draws the selected journey, and offers standard/satellite,
live-traffic and compressed playback controls. The SQLite database remains
WGS84 and AMap failure does not affect collection or statistics.

## Storage, backup and upgrade

- Database: `/data/journeys.db`
- SQLite mode: WAL with bounded busy timeout
- Backup mode: cold; Supervisor stops the app before copying its data
- Raw-point cleanup: automatic according to `retention_days`
- Journey summaries: retained even if old point detail has expired

Before changing versions, create a Home Assistant backup that includes this
app. Do not copy the live SQLite files while the app is running.

## Recovery

1. Stop Journey Analyzer.
2. Preserve or export `/data/journeys.db` if the data is needed.
3. Roll back or uninstall the app.
4. Remove the retained `homeassistant/*/journey_analyzer_*/config` discovery
   topics only if the entities must be removed.
5. Restore the pre-change Home Assistant backup only when a wider rollback is required.

Stopping this app does not modify Home Assistant's built-in Map, Recorder,
AMap dashboard, automations or location entities.
